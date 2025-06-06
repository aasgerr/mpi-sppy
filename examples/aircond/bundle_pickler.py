###############################################################################
# mpi-sppy: MPI-based Stochastic Programming in PYthon
#
# Copyright (c) 2024, Lawrence Livermore National Security, LLC, Alliance for
# Sustainable Energy, LLC, The Regents of the University of California, et al.
# All rights reserved. Please see the files COPYRIGHT.md and LICENSE.md for
# full copyright and license information.
###############################################################################
# Program to create proper bundles for aircond; DLW march 2022
# NOTE: As of 3 March 2022, you can't compare pickle bundle problems with non-pickled. See _demands_creator in aircondB.py for more discusion.
# see try_pickles.bash
# parallel version

import numpy as np
import mpisppy.tests.examples.aircondB as aircondB
from mpisppy.utils import config

from mpisppy import MPI

n_proc = MPI.COMM_WORLD.Get_size()
my_rank = MPI.COMM_WORLD.Get_rank()

# construct a node-scenario dictionary a priori for xhatspecific_spoke,
# according to naming convention for this problem

def _parse_args():
    cfg = config.Config()
    cfg.multistage()
    cfg.proper_bundle_config()
    aircondB.inparser_adder(cfg)
    cfg.parse_command_line("bundle_pickler for aircond")

    return cfg

def main():

    cfg = _parse_args()
    assert cfg.pickle_bundles_dir is not None
    assert cfg.scenarios_per_bundle is not None
    assert cfg.unpickle_bundles_dir is None

    BFs = cfg.branching_factors

    if BFs is None:
        raise RuntimeError("Branching factors must be specified")

    ScenCount = np.prod(BFs)

    kwargs = aircondB.kw_creator(cfg)

    bsize = int(cfg.scenarios_per_bundle)
    numbuns = ScenCount // bsize

    if numbuns < n_proc:
        raise RuntimeError(
            "More MPI ranks (%d) supplied than needed given the number of bundles (%d) "
            % (n_proc, numbuns)
        )

    avg = numbuns / n_proc
    slices = [list(range(int(i * avg), int((i + 1) * avg))) for i in range(n_proc)]

    local_slice = slices[my_rank]
    local_bundle_names = [f"Bundle_{bn*bsize}_{(bn+1)*bsize-1}" for bn in local_slice]

    #print(f"{slices=}")
    #print(f"{local_bundle_names=}")
    
    for bname in local_bundle_names:
        aircondB.scenario_creator(bname, **kwargs)
    

if __name__ == "__main__":
    main()
    
