###############################################################################
# mpi-sppy: MPI-based Stochastic Programming in PYthon
#
# Copyright (c) 2024, Lawrence Livermore National Security, LLC, Alliance for
# Sustainable Energy, LLC, The Regents of the University of California, et al.
# All rights reserved. Please see the files COPYRIGHT.md and LICENSE.md for
# full copyright and license information.
###############################################################################
import unittest
import sys
import os
import parapint
from mpisppy import MPI


comm = MPI.COMM_WORLD
rank = comm.Get_rank()


class TestSC(unittest.TestCase):
    def setUp(self):
        self.original_path = sys.path
        example_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'examples', 'farmer', 'schur')
        sys.path.append(example_dir)

    def tearDown(self):
        sys.path = self.original_path
    
    @unittest.expectedFailure
    def test_farmer_example(self):
        import schur_complement as sc_example

        linear_solver = parapint.linalg.MPISchurComplementLinearSolver(subproblem_solvers={ndx: parapint.linalg.ScipyInterface(compute_inertia=True) for ndx in range(3)},
                                                                       schur_complement_solver=parapint.linalg.ScipyInterface(compute_inertia=True))
        sc_opt = sc_example.solve_with_sc(scen_count=3, linear_solver=linear_solver)
        sc_sol = sc_opt.gather_var_values_to_rank0()

        if rank == 0:
            self.assertAlmostEqual(sc_sol[('Scenario0', 'DevotedAcreage[CORN0]')], 80, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario0', 'DevotedAcreage[SUGAR_BEETS0]')], 250, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario0', 'DevotedAcreage[WHEAT0]')], 170, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario1', 'DevotedAcreage[CORN0]')], 80, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario1', 'DevotedAcreage[SUGAR_BEETS0]')], 250, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario1', 'DevotedAcreage[WHEAT0]')], 170, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario2', 'DevotedAcreage[CORN0]')], 80, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario2', 'DevotedAcreage[SUGAR_BEETS0]')], 250, places=5)
            self.assertAlmostEqual(sc_sol[('Scenario2', 'DevotedAcreage[WHEAT0]')], 170, places=5)
