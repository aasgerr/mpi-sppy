###############################################################################
# mpi-sppy: MPI-based Stochastic Programming in PYthon
#
# Copyright (c) 2024, Lawrence Livermore National Security, LLC, Alliance for
# Sustainable Energy, LLC, The Regents of the University of California, et al.
# All rights reserved. Please see the files COPYRIGHT.md and LICENSE.md for
# full copyright and license information.
###############################################################################
# base class for hub and for spoke strata

import os
import logging
import time
import math
import inspect
import random
import pathlib

import numpy as np
from mpisppy import MPI

import pyomo.environ as pyo
from pyomo.opt import SolverFactory
from pyomo.common.collections import ComponentSet
from pyomo.solvers.plugins.solvers.gurobi_direct import GurobiDirect

from mpisppy.spbase import SPBase
import mpisppy.utils.sputils as sputils

from mpisppy.opt.presolve import SPPresolve

logger = logging.getLogger("SPOpt")
logger.setLevel(logging.WARN)


class SPOpt(SPBase):
    """ Defines optimization methods for hubs and spokes """

    def __init__(
            self,
            options,
            all_scenario_names,
            scenario_creator,
            scenario_denouement=None,
            all_nodenames=None,
            mpicomm=None,
            extensions=None,
            extension_kwargs=None,
            scenario_creator_kwargs=None,
            variable_probability=None,
            E1_tolerance=1e-5,
    ):
        super().__init__(
            options,
            all_scenario_names,
            scenario_creator,
            scenario_denouement=scenario_denouement,
            all_nodenames=all_nodenames,
            mpicomm=mpicomm,
            scenario_creator_kwargs=scenario_creator_kwargs,
            variable_probability=variable_probability,
        )
        self._save_active_objectives()
        self._subproblem_creation(options.get("verbose", False))
        if options.get("presolve", False):
            # NOTE: This creates another representation
            #       of each scenario subproblem in C++
            #       to presolve the model on. For large
            #       models, it is imperative we allow this
            #       object to get garbage collected to
            #       free the memory the C++ model uses.
            SPPresolve(self).presolve()
        self._create_fixed_nonant_cache()
        self.current_solver_options = None
        self.extensions = extensions
        self.extension_kwargs = extension_kwargs

        self._subproblem_solve_index = {}

        if self.options.get("solver_log_dir", None):
            if self.global_rank == 0:
                # create the directory if not there
                directory = self.options["solver_log_dir"]
                try:
                    pathlib.Path(directory).mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    raise FileExistsError(f"solver-log-dir={directory} already exists!")

        if (self.extensions is not None):
            if self.extension_kwargs is None:
                self.extobject = self.extensions(self)
            else:
                self.extobject = self.extensions(
                    self, **self.extension_kwargs
                )

    def _check_staleness(self, s):
        # check for staleness in *scenario* s
        # Look at the feasible scenarios. If we have any stale non-anticipative
        # variables, complain. Otherwise the user will hit this issue later when
        # we attempt to access the variables `value` attribute (see Issue #170).
        for v in s._mpisppy_data.nonant_indices.values():
            if (not v.fixed) and v.stale:
                if self.is_zero_prob(s, v) and v._value is None:
                    raise RuntimeError(
                            f"Non-anticipative zero-probability variable {v.name} "
                            f"on scenario {s.name} reported as stale and has no value. "
                             "Zero-probability variables must have a value (e.g., fixed).")
                else:
                    try:
                        float(pyo.value(v))
                    except Exception:
                        raise RuntimeError(
                            f"Non-anticipative variable {v.name} on scenario {s.name} "
                            "reported as stale. This usually means this variable "
                            "did not appear in any (active) components, and hence "
                            "was not communicated to the subproblem solver. ")


    def solve_one(self, solver_options, k, s,
                  dtiming=False,
                  gripe=False,
                  tee=False,
                  verbose=False,
                  disable_pyomo_signal_handling=False,
                  update_objective=True,
                  need_solution=True,
                  warmstart=sputils.WarmstartStatus.FALSE,
                  ):
        """ Solve one subproblem.

        Args:
            solver_options (dict or None):
                The scenario solver options.
            k (str):
                Subproblem name.
            s (ConcreteModel with appendages):
                The subproblem to solve.
            dtiming (boolean, optional):
                If True, reports timing values. Default False.
            gripe (boolean, optional):
                If True, outputs a message when a solve fails. Default False.
            tee (boolean, optional):
                If True, displays solver output. Default False.
            verbose (boolean, optional):
                If True, displays verbose output. Default False.
            disable_pyomo_signal_handling (boolean, optional):
                True for asynchronous PH; ignored for persistent solvers.
                Default False.
            update_objective (boolean, optional):
                If True, and a persistent solver is used, update
                the persistent solver's objective
            need_solution (boolean, optional):
                If True, raises an exception if a solution is not available.
                Default True
            warmstart (bool, optional):
                If True, warmstart the subproblem solves. Default False.

        Returns:
            float:
                Pyomo solve time in seconds.
        """


        def _vb(msg):
            if verbose and self.cylinder_rank == 0:
                print ("(rank0) " + msg)

        # if using a persistent solver plugin,
        # re-compile the objective due to changed weights and x-bars
        # high variance in set objective time (Feb 2023)?
        if update_objective and (sputils.is_persistent(s._solver_plugin)):
            set_objective_start_time = time.time()
            active_objective = sputils.find_active_objective(s)
            s._solver_plugin.set_objective(active_objective)
            set_objective_time = time.time() - set_objective_start_time
        else:
            set_objective_time = 0

        if self.extensions is not None:
            results = self.extobject.pre_solve(s)

        solve_start_time = time.time()
        if (solver_options):
            _vb("Using sub-problem solver options="
                + str(solver_options))
            for option_key,option_value in solver_options.items():
                s._solver_plugin.options[option_key] = option_value

        solve_keyword_args = dict()
        if self.cylinder_rank == 0:
            if tee is not None and tee is True:
                solve_keyword_args["tee"] = True
        if (sputils.is_persistent(s._solver_plugin)):
            solve_keyword_args["save_results"] = False
        if self.options.get("warmstart_subproblems", False):
            if warmstart == sputils.WarmstartStatus.USER_SOLUTION:
                warmstart = self.options.get("user_warmstart", False)
            elif warmstart == sputils.WarmstartStatus.PRIOR_SOLUTION:
                warmstart = getattr(s._mpisppy_data, "solution_available", False)
            else:
                assert isinstance(warmstart, (sputils.WarmstartStatus, bool))
                warmstart = bool(warmstart)
            solve_keyword_args["warmstart"] = warmstart
        if disable_pyomo_signal_handling:
            # solve_keyword_args["use_signal_handling"] = False
            pass

        if self.options.get("solver_log_dir", None):
            if k not in self._subproblem_solve_index:
                self._subproblem_solve_index[k] = 0
            dir_name = self.options["solver_log_dir"]
            file_name = f"{self._get_cylinder_name()}_{k}_{self._subproblem_solve_index[k]}.log"
            # Workaround for Pyomo/pyomo#3589: Setting 'keepfiles' to True is required
            # for proper functionality when using the GurobiDirect / GurobiPersistent solver.
            if isinstance(s._solver_plugin, GurobiDirect):
                s._solver_plugin.options["LogFile"] = os.path.join(dir_name, file_name)
            else:
                solve_keyword_args["logfile"] = os.path.join(dir_name, file_name)
            self._subproblem_solve_index[k] += 1

        Ag = getattr(self, "Ag", None)  # agnostic
        if Ag is not None:
            assert not disable_pyomo_signal_handling, "Not thinking about agnostic APH yet"
            kws = {"s": s, "solve_keyword_args": solve_keyword_args, "gripe": gripe, "tee": tee, "need_solution": need_solution}
            Ag.callout_agnostic(kws)  # not going to use the return values
        else:
            #  didcallout = False  (returned true by the callout, but not used)
            try:
                results = s._solver_plugin.solve(s,
                                                 **solve_keyword_args,
                                                 load_solutions=False)
                solver_exception = None
            except Exception as e:
                results = None
                solver_exception = e

            if sputils.not_good_enough_results(results):
                s._mpisppy_data.scenario_feasible = False

                if gripe:
                    print (f"[{self._get_cylinder_name()}] Solve failed for scenario {s.name}")
                    if results is not None:
                        print ("status=", results.solver.status)
                        print ("TerminationCondition=",
                               results.solver.termination_condition)
                    else:
                        print("no results object, so solving agin with tee=True")
                        solve_keyword_args["tee"] = True
                        results = s._solver_plugin.solve(s,
                                                 **solve_keyword_args,
                                                 load_solutions=False)

                if solver_exception is not None:
                    raise solver_exception

            else:
                try:
                    if sputils.is_persistent(s._solver_plugin):
                        s._solver_plugin.load_vars()
                    else:
                        s.solutions.load_from(results)
                except Exception as e: # catch everything
                    if need_solution:
                        raise e
                    else:
                        s._mpisppy_data.solution_available = False
                else:
                    s._mpisppy_data.solution_available = True
                if self.is_minimizing:
                    s._mpisppy_data.outer_bound = results.Problem[0].Lower_bound
                    s._mpisppy_data.inner_bound = results.Problem[0].Upper_bound
                else:
                    s._mpisppy_data.outer_bound = results.Problem[0].Upper_bound
                    s._mpisppy_data.inner_bound = results.Problem[0].Lower_bound
                s._mpisppy_data.scenario_feasible = True
            # TBD: get this ready for IPopt (e.g., check feas_prob every time)
            # propogate down
            if self.bundling: # must be a bundle
                for sname in s._ef_scenario_names:
                     self.local_scenarios[sname]._mpisppy_data.scenario_feasible\
                         = s._mpisppy_data.scenario_feasible
                     if s._mpisppy_data.scenario_feasible:
                         self._check_staleness(self.local_scenarios[sname])
            else:  # not a bundle
                if s._mpisppy_data.scenario_feasible:
                    self._check_staleness(s)

        # end of Agnostic bypass

        # Time capture moved down August 2023
        pyomo_solve_time = time.time() - solve_start_time
        
        if self.extensions is not None:
            results = self.extobject.post_solve(s, results)

        return pyomo_solve_time + set_objective_time  # set_objective_time added Feb 2023


    def solve_loop(self, solver_options=None,
                   use_scenarios_not_subproblems=False,
                   dtiming=False,
                   gripe=False,
                   disable_pyomo_signal_handling=False,
                   tee=False,
                   verbose=False,
                   need_solution=True,
                   warmstart=sputils.WarmstartStatus.FALSE,
                   ):
        """ Loop over `local_subproblems` and solve them in a manner
        dicated by the arguments.

        In addition to changing the Var values in the scenarios, this function
        also updates the `_PySP_feas_indictor` to indicate which scenarios were
        feasible/infeasible.

        Args:
            solver_options (dict, optional):
                The scenario solver options.
            use_scenarios_not_subproblems (boolean, optional):
                If True, solves individual scenario problems, not subproblems.
                This distinction matters when using bundling. Default is False.
            dtiming (boolean, optional):
                If True, reports solve timing information. Default is False.
            gripe (boolean, optional):
                If True, output a message when a solve fails. Default is False.
            disable_pyomo_signal_handling (boolean, optional):
                True for asynchronous PH; ignored for persistent solvers.
                Default False.
            tee (boolean, optional):
                If True, displays solver output. Default False.
            verbose (boolean, optional):
                If True, displays verbose output. Default False.
            need_solution (boolean, optional):
                If True, raises an exception if a solution is not available.
                Default True
            warmstart (bool, optional):
                If True, warmstart the subproblem solves. Default False.
        """

        """ Developer notes:

        This function assumes that every scenario already has a
        `_solver_plugin` attached.

        I am not sure what happens with solver_options None for a persistent
        solver. Do options persist?

        set_objective takes care of W and prox changes.
        """
        def _vb(msg):
            if verbose and self.cylinder_rank == 0:
                print ("(rank0) " + msg)
        _vb("Entering solve_loop function.")
        logger.debug("  early solve_loop for rank={}".format(self.cylinder_rank))

        if self.extensions is not None:
                self.extobject.pre_solve_loop()

        # note that when there is no bundling, scenarios are subproblems
        if use_scenarios_not_subproblems:
            s_source = self.local_scenarios
        else:
            s_source = self.local_subproblems
        pyomo_solve_times = list()
        for k,s in s_source.items():
            logger.debug("  in loop solve_loop k={}, rank={}".format(k, self.cylinder_rank))
            if tee:
                print(f"Tee solve for {k} on global rank {self.global_rank}")
            pyomo_solve_times.append(
                self.solve_one(
                    solver_options,
                    k,
                    s,
                    dtiming=dtiming,
                    verbose=verbose,
                    tee=tee,
                    gripe=gripe,
                    disable_pyomo_signal_handling=disable_pyomo_signal_handling,
                    need_solution=need_solution,
                    warmstart=warmstart,
                )
            )

        if self.extensions is not None:
                self.extobject.post_solve_loop()

        if dtiming:
            all_pyomo_solve_times = self.mpicomm.gather(pyomo_solve_times, root=0)
            if self.cylinder_rank == 0:
                apst = [pst for l_pst in all_pyomo_solve_times for pst in l_pst]
                print("Pyomo solve times (seconds):")
                print("\tmin=%4.2f@%d mean=%4.2f max=%4.2f@%d" %
                      (np.min(apst), np.argmin(apst),
                       np.mean(apst),
                       np.max(apst), np.argmax(apst)))


    def Eobjective(self, verbose=False):
        """ Compute the expected objective function across all scenarios.

        Note:
            Assumes the optimization is done beforehand,
            therefore DOES NOT CHECK FEASIBILITY or NON-ANTICIPATIVITY!
            This method uses whatever the current value of the objective
            function is.

        Args:
            verbose (boolean, optional):
                If True, displays verbose output. Default False.

        Returns:
            float:
                The expected objective function value
        """
        local_Eobjs = []
        for k,s in self.local_scenarios.items():
            objfct = self.saved_objectives[k]  # if bundling?
            Ag = getattr(self, "Ag", None)
            if Ag is None:
                local_Eobjs.append(s._mpisppy_probability * pyo.value(objfct))
            else:
                # Agnostic will have attached the objective (and doesn't bundle as of Aug 2023)
                local_Eobjs.append(s._mpisppy_probability * s._mpisppy_data.inner_bound)
            if verbose:
                print ("caller", inspect.stack()[1][3])
                print ("E_Obj Scenario {}, prob={}, Obj={}, ObjExpr={}"\
                       .format(k, s._mpisppy_probability, pyo.value(objfct), objfct.expr))

        local_Eobj = np.array([math.fsum(local_Eobjs)])
        global_Eobj = np.zeros(1)
        self.mpicomm.Allreduce(local_Eobj, global_Eobj, op=MPI.SUM)

        return global_Eobj[0]


    def Ebound(self, verbose=False, extra_sum_terms=None):
        """ Compute the expected outer bound across all scenarios.

        Note:
            Assumes the optimization is done beforehand.
            Uses whatever bound is currently  attached to the subproblems.

        Args:
            verbose (boolean):
                If True, displays verbose output. Default False.
            extra_sum_terms: (None or iterable)
                If iterable, additional terms to put in the floating-point
                sum reduction

        Returns:
            float:
                The expected objective outer bound.
        """
        local_Ebounds = []
        for k,s in self.local_subproblems.items():
            logger.debug("  in loop Ebound k={}, rank={}".format(k, self.cylinder_rank))
            try:
                eb = s._mpisppy_probability * float(s._mpisppy_data.outer_bound)
            except:
                print(f"eb calc failed for {s._mpisppy_probability} * {s._mpisppy_data.outer_bound}")
                raise
            local_Ebounds.append(eb)
            if verbose:
                print ("caller", inspect.stack()[1][3])
                print ("E_Bound Scenario {}, prob={}, bound={}"\
                       .format(k, s._mpisppy_probability, s._mpisppy_data.outer_bound))

        if extra_sum_terms is not None:
            local_Ebound_list = [math.fsum(local_Ebounds)] + list(extra_sum_terms)
        else:
            local_Ebound_list = [math.fsum(local_Ebounds)]

        local_Ebound = np.array(local_Ebound_list)
        global_Ebound = np.zeros(len(local_Ebound_list))

        self.mpicomm.Allreduce(local_Ebound, global_Ebound, op=MPI.SUM)

        if extra_sum_terms is None:
            return global_Ebound[0]
        else:
            return global_Ebound[0], global_Ebound[1:]


    def _update_E1(self):
        """ Add up the probabilities of all scenarios using a reduce call.
            then attach it to the PH object as a float.
        """
        localP = np.zeros(1, dtype='d')
        globalP = np.zeros(1, dtype='d')

        for k,s in self.local_scenarios.items():
            localP[0] +=  s._mpisppy_probability

        self.mpicomm.Allreduce([localP, MPI.DOUBLE],
                           [globalP, MPI.DOUBLE],
                           op=MPI.SUM)

        self.E1 = float(globalP[0])


    def feas_prob(self):
        """ Compute the total probability of all feasible scenarios.

        This function can be used to check whether all scenarios are feasible
        by comparing the return value to one.

        Note:
            This function assumes the scenarios have a boolean
            `_mpisppy_data.scenario_feasible` attribute.

        Returns:
            float:
                Sum of the scenario probabilities over all feasible scenarios.
                This value equals E1 if all scenarios are feasible.
        """

        # locals[0] is E_feas and locals[1] is E_1
        locals = np.zeros(1, dtype='d')
        globals = np.zeros(1, dtype='d')

        for k,s in self.local_scenarios.items():
            if s._mpisppy_data.scenario_feasible:
                locals[0] += s._mpisppy_probability

        self.mpicomm.Allreduce([locals, MPI.DOUBLE],
                           [globals, MPI.DOUBLE],
                           op=MPI.SUM)

        return float(globals[0])


    def infeas_prob(self):
        """ Sum the total probability for all infeasible scenarios.

        Note:
            This function assumes the scenarios have a boolean
            `_mpisppy_data.scenario_feasible` attribute.

        Returns:
            float:
                Sum of the scenario probabilities over all infeasible scenarios.
                This value equals 0 if all scenarios are feasible.
        """

        locals = np.zeros(1, dtype='d')
        globals = np.zeros(1, dtype='d')

        for k,s in self.local_scenarios.items():
            if not s._mpisppy_data.scenario_feasible:
                locals[0] += s._mpisppy_probability

        self.mpicomm.Allreduce([locals, MPI.DOUBLE],
                           [globals, MPI.DOUBLE],
                           op=MPI.SUM)

        return float(globals[0])


    def avg_min_max(self, compstr):
        """ Can be used to track convergence progress.

        Args:
            compstr (str):
                The name of the Pyomo component. Should not be indexed.

        Returns:
            tuple:
                Tuple containing

                avg (float):
                    Average across all scenarios.
                min (float):
                    Minimum across all scenarios.
                max (float):
                    Maximum across all scenarios.

        Note:
            WARNING: Does a Allreduce.
            Not user-friendly. If you give a bad compstr, it will just crash.
        """
        firsttime = True
        localavg = np.zeros(1, dtype='d')
        localmin = np.zeros(1, dtype='d')
        localmax = np.zeros(1, dtype='d')
        globalavg = np.zeros(1, dtype='d')
        globalmin = np.zeros(1, dtype='d')
        globalmax = np.zeros(1, dtype='d')

        v_cuid = pyo.ComponentUID(compstr)

        for k,s in self.local_scenarios.items():

            compv = pyo.value(v_cuid.find_component_on(s))


            ###compv = pyo.value(getattr(s, compstr))
            localavg[0] += s._mpisppy_probability * compv
            if compv < localmin[0] or firsttime:
                localmin[0] = compv
            if compv > localmax[0] or firsttime:
                localmax[0] = compv
            firsttime = False

        self.comms["ROOT"].Allreduce([localavg, MPI.DOUBLE],
                                     [globalavg, MPI.DOUBLE],
                                     op=MPI.SUM)
        self.comms["ROOT"].Allreduce([localmin, MPI.DOUBLE],
                                     [globalmin, MPI.DOUBLE],
                                     op=MPI.MIN)
        self.comms["ROOT"].Allreduce([localmax, MPI.DOUBLE],
                                     [globalmax, MPI.DOUBLE],
                                     op=MPI.MAX)
        return (float(globalavg[0]),
                float(globalmin[0]),
                float(globalmax[0]))


    def _put_nonant_cache(self, cache):
        """ Put the value in the cache for nonants *for all local scenarios*
        Args:
            cache (np vector) to receive the nonant's for all local scenarios

        """
        ci = 0 # Cache index
        for sname, model in self.local_scenarios.items():
            if model._mpisppy_data.nonant_cache is None:
                raise RuntimeError(f"Rank {self.global_rank} Scenario {sname}"
                                   " nonant_cache is None"
                                   " (call _save_nonants first?)")
            for i,_ in enumerate(model._mpisppy_data.nonant_indices):
                assert(ci < len(cache))
                model._mpisppy_data.nonant_cache[i] = cache[ci]
                ci += 1


    def _restore_original_fixedness(self):
        # We are going to hack a little to get the original fixedness, but current values
        # (We are assuming that algorithms are not fixing anticipative vars; but if they
        # do, they had better put their fixedness back to its correct state.)
        self._save_nonants()
        for k,s in self.local_scenarios.items():
            for ci, _ in enumerate(s._mpisppy_data.nonant_indices):
                s._mpisppy_data.fixedness_cache[ci] = s._mpisppy_data.original_fixedness[ci]
            Ag = getattr(self, "Ag", None)
            if Ag is not None:
                Ag.callout_agnostic({"s": s})
        self._restore_nonants()


    def _fix_nonants(self, cache):
        """ Fix the Vars subject to non-anticipativity at given values.
            Loop over the scenarios to restore, but loop over subproblems
            to alert persistent solvers.
        Args:
            cache (ndn dict of list or numpy vector): values at which to fix
        WARNING:
            We are counting on Pyomo indices not to change order between
            when the cache_list is created and used.
        NOTE:
            You probably want to call _save_nonants right before calling this
        """
        rounding_bias = self.options.get("rounding_bias", 0.0)
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if (sputils.is_persistent(s._solver_plugin)):
                persistent_solver = s._solver_plugin

            nlens = s._mpisppy_data.nlens
            for node in s._mpisppy_node_list:
                ndn = node.name
                if ndn not in cache:
                    raise RuntimeError("Could not find {} in {}"\
                                       .format(ndn, cache))
                if cache[ndn] is None:
                    raise RuntimeError("Empty cache for scen={}, node={}".format(k, ndn))
                if len(cache[ndn]) != nlens[ndn]:
                    raise RuntimeError("Needed {} nonant Vars for {}, got {}"\
                                       .format(nlens[ndn], ndn, len(cache[ndn])))
                for i in range(nlens[ndn]):
                    this_vardata = node.nonant_vardata_list[i]
                    if this_vardata in node.surrogate_vardatas:
                        continue
                    if this_vardata.is_binary() or this_vardata.is_integer():
                        this_vardata._value = round(cache[ndn][i] + rounding_bias)
                    else:
                        this_vardata._value = cache[ndn][i]
                    this_vardata.fix()
                    if persistent_solver is not None:
                        persistent_solver.update_var(this_vardata)

            Ag = getattr(self, "Ag", None)
            if Ag is not None:
                Ag.callout_agnostic({"s": s})

    def _fix_root_nonants(self,root_cache):
        """ Fix the 1st stage Vars subject to non-anticipativity at given values.
            Loop over the scenarios to restore, but loop over subproblems
            to alert persistent solvers.
            Useful for multistage to find feasible solutions with a given scenario.
        Args:
            root_cache (numpy vector): values at which to fix
        WARNING:
            We are counting on Pyomo indices not to change order between
            when the cache_list is created and used.
        NOTE:
            You probably want to call _save_nonants right before calling this
        """
        rounding_bias = self.options.get("rounding_bias", 0.0)
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if (sputils.is_persistent(s._solver_plugin)):
                persistent_solver = s._solver_plugin

            nlens = s._mpisppy_data.nlens

            rootnode = None
            for node in s._mpisppy_node_list:
                if node.name == 'ROOT':
                    rootnode = node
                    break

            if rootnode is None:
                raise RuntimeError("Could not find a 'ROOT' node in scen {}"\
                                   .format(k))
            if root_cache is None:
                raise RuntimeError("Empty root cache for scen={}".format(k))
            if len(root_cache) != nlens['ROOT']:
                raise RuntimeError("Needed {} nonant Vars for 'ROOT', got {}"\
                                   .format(nlens['ROOT'], len(root_cache)))

            for i in range(nlens['ROOT']):
                this_vardata = node.nonant_vardata_list[i]
                if this_vardata in node.surrogate_vardatas:
                    continue
                if this_vardata.is_binary() or this_vardata.is_integer():
                    this_vardata._value = round(root_cache[i] + rounding_bias)
                else:
                    this_vardata._value = root_cache[i]
                this_vardata.fix()
                if persistent_solver is not None:
                    persistent_solver.update_var(this_vardata)
                        
            Ag = getattr(self, "Ag", None)
            if Ag is not None:
                Ag.callout_agnostic({"s": s})


    def _restore_nonants(self, update_persistent=True):
        """ Restore nonanticipative variables to their original values.

        This function works in conjunction with _save_nonants.

        We loop over the scenarios to restore variables, but loop over
        subproblems to alert persistent solvers.

        Warning:
            We are counting on Pyomo indices not to change order between save
            and restoration. THIS WILL NOT WORK ON BUNDLES (Feb 2019) but
            hopefully does not need to.
        """
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if (update_persistent and sputils.is_persistent(s._solver_plugin)):
                persistent_solver = s._solver_plugin

            for ci, vardata in enumerate(s._mpisppy_data.nonant_indices.values()):
                vardata._value = s._mpisppy_data.nonant_cache[ci]
                vardata.fixed = s._mpisppy_data.fixedness_cache[ci]

                if persistent_solver is not None:
                    persistent_solver.update_var(vardata)
                    
            Ag = getattr(self, "Ag", None)
            if Ag is not None:
                Ag.callout_agnostic({"s": s})


    def _save_nonants(self):
        """ Save the values and fixedness status of the Vars that are
        subject to non-anticipativity.

        Note:
            Assumes nonant_cache is on the scenarios and can be used
            as a list, or puts it there.
        Warning:
            We are counting on Pyomo indices not to change order before the
            restoration. We also need the Var type to remain stable.
        Note:
            The value cache is np because it might be transmitted
        """
        for k,s in self.local_scenarios.items():
            nlens = s._mpisppy_data.nlens
            if not hasattr(s._mpisppy_data,"nonant_cache"):
                clen = sum(nlens[ndn] for ndn in nlens)
                s._mpisppy_data.nonant_cache = np.zeros(clen, dtype='d')
                s._mpisppy_data.fixedness_cache = [None for _ in range(clen)]

            for ci, xvar in enumerate(s._mpisppy_data.nonant_indices.values()):
                s._mpisppy_data.nonant_cache[ci]  = xvar._value
                s._mpisppy_data.fixedness_cache[ci]  = xvar.is_fixed()


    def _save_original_nonants(self):
        """ Save the current value of the nonanticipative variables.

        Values are saved in the `_PySP_original_nonants` attribute. Whether
        the variable was fixed is stored in `_PySP_original_fixedness`.
        """
        for k,s in self.local_scenarios.items():
            if not hasattr(s._mpisppy_data,"nonant_cache"):
                # uses nonant cache to signal other things have not
                # been created
                # TODO: combine cache creation (or something else)
                clen = len(s._mpisppy_data.nonant_indices)
                s._mpisppy_data.original_fixedness = [None] * clen
                s._mpisppy_data.original_nonants = np.zeros(clen, dtype='d')

            for ci, xvar in enumerate(s._mpisppy_data.nonant_indices.values()):
                s._mpisppy_data.original_fixedness[ci]  = xvar.is_fixed()
                s._mpisppy_data.original_nonants[ci]  = xvar._value


    def _restore_original_nonants(self):
        """ Restore nonanticipative variables to their original values.

        This function works in conjunction with _save_original_nonants.

        We loop over the scenarios to restore variables, but loop over
        subproblems to alert persistent solvers.

        Warning:
            We are counting on Pyomo indices not to change order between save
            and restoration. THIS WILL NOT WORK ON BUNDLES (Feb 2019) but
            hopefully does not need to.
        """
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if not self.bundling:
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin
            else:
                raise RuntimeError("restore_original_nonants called for a bundle")
            for ci, vardata in enumerate(s._mpisppy_data.nonant_indices.values()):
                vardata._value = s._mpisppy_data.original_nonants[ci]
                vardata.fixed = s._mpisppy_data.original_fixedness[ci]
                if persistent_solver is not None:
                    persistent_solver.update_var(vardata)


    def _save_active_objectives(self):
        """ Save the active objectives for use in PH, bundles, and calculation """
        self.saved_objectives = dict()

        for sname, scenario_instance in self.local_scenarios.items():
            self.saved_objectives[sname] = sputils.find_active_objective(scenario_instance)


    def FormEF(self, scen_dict, EF_name=None):
        """ Make the EF for a list of scenarios.

        This function is mainly to build bundles. To build (and solve) the
        EF of the entire problem, use the EF class instead.

        Args:
            scen_dict (dict):
                Subset of local_scenarios; the scenarios to put in the EF. THe
                dictionary maps sccneario names (strings) to scenarios (Pyomo
                concrete model objects).
            EF_name (string, optional):
                Name for the resulting EF model.

        Returns:
            :class:`pyomo.environ.ConcreteModel`:
                The EF with explicit non-anticipativity constraints.

        Raises:
            RuntimeError:
                If the `scen_dict` is empty, or one of the scenarios in
                `scen_dict` is not owned locally (i.e. is not in
                `local_scenarios`).

        Note:
            We attach a list of the scenario names called _PySP_subsecen_names
        Note:
            We deactivate the objective on the scenarios.
        Note:
            The scenarios are sub-blocks, so they naturally get the EF solution
            Also the EF objective references Vars and Parms on the scenarios
            and hence is automatically updated when the scenario
            objectives are. THIS IS ALL CRITICAL to bundles.
            xxxx TBD: ask JP about objective function transmittal to persistent solvers
        Note:
            Objectives are scaled (normalized) by _mpisppy_probability
        """
        # The individual scenario instances are sub-blocks of the binding
        # instance. Needed to facilitate bundles + persistent solvers
        if len(scen_dict) == 0:
            raise RuntimeError("Empty scenario list for EF")

        if len(scen_dict) == 1:
            sname, scenario_instance = list(scen_dict.items())[0]
            if EF_name is not None:
                print ("WARNING: EF_name="+EF_name+" not used; singleton="+sname)
                print ("MAJOR WARNING: a bundle of size one encountered; if you try to compute bounds it might crash (Feb 2019)")
            return scenario_instance

        EF_instance = sputils._create_EF_from_scen_dict(scen_dict, EF_name=EF_name,
                        nonant_for_fixed_vars=False)
        return EF_instance


    def _subproblem_creation(self, verbose=False):
        """ Create local subproblems (not local scenarios).

        If bundles are specified, this function creates the bundles.
        Otherwise, this function simply copies pointers to the already-created
        `local_scenarios`.

        Args:
            verbose (boolean, optional):
                If True, displays verbose output. Default False.
        """
        self.local_subproblems = dict()
        if self.bundling:
            rank_local = self.cylinder_rank
            for bun in self.names_in_bundles[rank_local]:
                sdict = dict()
                bname = "rank" + str(self.cylinder_rank) + "bundle" + str(bun)
                for sname in self.names_in_bundles[rank_local][bun]:
                    if (verbose and self.cylinder_rank==0):
                        print ("bundling "+sname+" into "+bname)
                    scen = self.local_scenarios[sname]
                    scen._mpisppy_data.bundlename = bname
                    sdict[sname] = scen
                self.local_subproblems[bname] = self.FormEF(sdict, bname)
                self.local_subproblems[bname].scen_list = \
                    self.names_in_bundles[rank_local][bun]
                self.local_subproblems[bname]._mpisppy_probability = \
                                    sum(s._mpisppy_probability for s in sdict.values())
        else:
            for sname, s in self.local_scenarios.items():
                self.local_subproblems[sname] = s
                self.local_subproblems[sname].scen_list = [sname]


    def _create_solvers(self, presolve=True):

        dtiming = ("display_timing" in self.options) and self.options["display_timing"]
        local_sit = [] # Local set instance time for time tracking
        for sname, s in self.local_subproblems.items(): # solver creation
            s._solver_plugin = SolverFactory(self.options["solver_name"])
            if (sputils.is_persistent(s._solver_plugin)):
                if dtiming:
                    set_instance_start_time = time.time()

                set_instance_retry(s, s._solver_plugin, sname)

                if dtiming:
                    local_sit.append( time.time() - set_instance_start_time )
            else:
                if dtiming:
                    local_sit.append(0.0)

            ## if we have bundling, attach
            ## the solver plugin to the scenarios
            ## as well to avoid some gymnastics
            if self.bundling:
                for scen_name in s.scen_list:
                    scen = self.local_scenarios[scen_name]
                    scen._solver_plugin = s._solver_plugin
        if dtiming:
            all_set_instance_times = self.mpicomm.gather(local_sit,
                                                     root=0)
            if self.cylinder_rank == 0:
                asit = [sit for l_sit in all_set_instance_times for sit in l_sit]
                if len(asit) == 0:
                    print("Set instance times not available.")
                else:
                    print("Set instance times: \tmin=%4.2f mean=%4.2f max=%4.2f" %
                      (np.min(asit), np.mean(asit), np.max(asit)))

    def _create_fixed_nonant_cache(self):
        self._initial_fixed_varibles = ComponentSet()
        for s in self.local_scenarios.values():
            for v in s._mpisppy_data.nonant_indices.values():
                if v.fixed:
                    self._initial_fixed_varibles.add(v)

    def _can_update_best_bound(self):
        for s in self.local_scenarios.values():
            for v in s._mpisppy_data.nonant_indices.values():
                if v.fixed:
                    if v not in self._initial_fixed_varibles:
                        return False
        return True

    def subproblem_scenario_generator(self):
        """
        Iterate over every scenario, yielding the
        subproblem_name, subproblem, scenario_name, scenario.

        Useful for managing bundles
        """
        for sub_name, sub in self.local_subproblems.items():
            for s_name in sub.scen_list:
                yield sub_name, sub, s_name, self.local_scenarios[s_name]


# these parameters should eventually be promoted to a non-PH
# general class / location. even better, the entire retry
# logic can be encapsulated in a sputils.py function.
MAX_ACQUIRE_LICENSE_RETRY_ATTEMPTS = 5

def set_instance_retry(subproblem, solver_plugin, subproblem_name):

    sname = subproblem_name
    # this loop is required to address the sitution where license
    # token servers become temporarily over-subscribed / non-responsive
    # when large numbers of ranks are in use.

    num_retry_attempts = 0
    while True:
        try:
            solver_plugin.set_instance(subproblem)
            if num_retry_attempts > 0:
                print("Acquired solver license (call to set_instance() for scenario=%s) after %d retry attempts" % (sname, num_retry_attempts))
            break
        # pyomo presently has no general way to trap a license acquisition
        # error - so we're stuck with trapping on "any" exception. not ideal.
        except Exception:
            if num_retry_attempts == 0:
                print("Failed to acquire solver license (call to set_instance() for scenario=%s) after first attempt" % (sname))
            else:
                print("Failed to acquire solver license (call to set_instance() for scenario=%s) after %d retry attempts" % (sname, num_retry_attempts))
            if num_retry_attempts == MAX_ACQUIRE_LICENSE_RETRY_ATTEMPTS:
                raise RuntimeError("Failed to acquire solver license - call to set_instance() for scenario=%s failed after %d retry attempts" % (sname, num_retry_attempts))
            else:
                sleep_time = random.random()
                print(f"Sleeping for {sleep_time:.2f} seconds before re-attempting")
                time.sleep(sleep_time)
                num_retry_attempts += 1
