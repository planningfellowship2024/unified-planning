# Copyright 2021 AIPlan4EU project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""This module defines an interface for a generic PDDL planner."""


import asyncio
from asyncio.subprocess import PIPE
import select
import sys
import tempfile
import os
import re
import subprocess
import time
import unified_planning as up
import unified_planning.solvers as solvers
from unified_planning.shortcuts import *
from unified_planning.solvers.results import PlanGenerationResult
from unified_planning.io.pddl_writer import PDDLWriter
from unified_planning.exceptions import UPException
from typing import IO, Any, Callable, Optional, List, Tuple, cast


class PDDLSolver(solvers.solver.Solver):
    """
    This class is the interface of a generic PDDL solver
    that can be invocated through a subprocess call.
    """

    def __init__(self, needs_requirements=True):
        solvers.solver.Solver.__init__(self)
        self._needs_requirements = needs_requirements

    @staticmethod
    def is_oneshot_planner() -> bool:
        return True

    def _get_cmd(self, domain_filename: str, problem_filename: str, plan_filename: str) -> List[str]:
        '''Takes in input two filenames where the problem's domain and problem are written, a
        filename where to write the plan and returns a list of command to run the solver on the
        problem and write the plan on the file called plan_filename.'''
        raise NotImplementedError

    def _plan_from_file(self, problem: 'up.model.Problem', plan_filename: str) -> 'up.plan.Plan':
        '''Takes a problem and a filename and returns the plan parsed from the file.'''
        actions = []
        with open(plan_filename) as plan:
            for line in plan.readlines():
                if re.match(r'^\s*(;.*)?$', line):
                    continue
                res = re.match(r'^\s*\(\s*([\w?-]+)((\s+[\w?-]+)*)\s*\)\s*$', line)
                if res:
                    action = problem.action(res.group(1))
                    parameters = []
                    for p in res.group(2).split():
                        parameters.append(ObjectExp(problem.object(p)))
                    actions.append(up.plan.ActionInstance(action, tuple(parameters)))
                else:
                    raise UPException('Error parsing plan generated by ' + self.__class__.__name__)
        return up.plan.SequentialPlan(actions)

    def solve(self, problem: 'up.model.Problem',
                callback: Optional[Callable[['up.solvers.results.PlanGenerationResult'], None]] = None,
                timeout: Optional[float] = None,
                output_stream: Optional[IO[str]] = None) -> 'up.solvers.results.PlanGenerationResult':
        w = PDDLWriter(problem, self._needs_requirements)
        plan = None
        logs: List['up.solvers.results.LogMessage'] = []
        with tempfile.TemporaryDirectory() as tempdir:
            domanin_filename = os.path.join(tempdir, 'domain.pddl')
            problem_filename = os.path.join(tempdir, 'problem.pddl')
            plan_filename = os.path.join(tempdir, 'plan.txt')
            w.write_domain(domanin_filename)
            w.write_problem(problem_filename)
            cmd = self._get_cmd(domanin_filename, problem_filename, plan_filename)

            try:
                if sys.platform == "win32":
                    loop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                timeout_occurred, (proc_out, proc_err), retval = loop.run_until_complete(run_command(cmd, timeout=timeout, output_stream=output_stream))
            finally:
                loop.close()

            logs.append(up.solvers.results.LogMessage(up.solvers.results.INFO, ''.join(proc_out)))
            logs.append(up.solvers.results.LogMessage(up.solvers.results.ERROR, ''.join(proc_err)))
            if os.path.isfile(plan_filename):
                plan = self._plan_from_file(problem, plan_filename)
            if timeout_occurred and retval != 0:
                return PlanGenerationResult(up.solvers.results.TIMEOUT, plan=plan, log_messages=logs, planner_name=self.name)
        status: int = self._result_status(problem, plan)
        return PlanGenerationResult(status, plan, log_messages=logs, planner_name=self.name)

    def _result_status(self, problem: 'up.model.Problem', plan: Optional['up.plan.Plan']) -> int:
        '''Takes a problem and a plan and returns the status that represents this plan.
        The possible status with their interpretation can be found in the up.plan file.'''
        raise NotImplementedError

    def destroy(self):
        pass


async def run_command(cmd, timeout: Optional[float] = None, output_stream: Optional[IO[str]] = None) -> Tuple[bool, Tuple[List[str], List[str]], int]:
    start = time.time()
    process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)

    timeout_occoured = False
    process_output: Tuple[List[str], List[str]] = ([], []) #stdout, stderr
    while True:
        lines = [b"", b""]
        oks = [True, True]
        for idx, stream in enumerate([process.stdout, process.stderr]):
            assert stream is not None
            try:
                lines[idx] = await asyncio.wait_for(stream.readline(), 0.5)
            except asyncio.TimeoutError:
                oks[idx] = False

        if all(oks) and (not lines[0] and not lines[1]): # EOF
            break
        else:
            for idx in range(2):
                output_string = lines[idx].decode().replace('\r\n', '\n')
                if output_stream is not None:
                    cast(IO[str], output_stream).write(output_string)
                process_output[idx].append(output_string)
        if timeout is not None and time.time() - start >= timeout:
            try:
                process.kill()
            except OSError:
                pass # This can happen if the process is already terminated
            timeout_occoured = True
            break

    await process.wait() # Wait for the child process to exit
    return timeout_occoured, process_output, cast(int, process.returncode)
