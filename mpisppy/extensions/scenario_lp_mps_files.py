###############################################################################
# mpi-sppy: MPI-based Stochastic Programming in PYthon
#
# Copyright (c) 2024, Lawrence Livermore National Security, LLC, Alliance for
# Sustainable Energy, LLC, The Regents of the University of California, et al.
# All rights reserved. Please see the files COPYRIGHT.md and LICENSE.md for
# full copyright and license information.
###############################################################################
""" Extension to write an lp file for each scenario and a json file for
the nonant structure for each scenario (yes, for two stage problems this
json file will be the same for all scenarios.)
"""
import os
import json
import mpisppy.extensions.extension
import pyomo.core.base.label as pyomo_label


def lpize(varname):
    # convert varname to the string that will appear in the lp and mps files
    return pyomo_label.cpxlp_label_from_name(varname)


class Scenario_lp_mps_files(mpisppy.extensions.extension.Extension):

    def __init__(self, ph):
        self.ph = ph
        self.dirname = self.ph.options["write_lp_mps_extension_options"].get("write_scenario_lp_mps_files_dir")
        if self.dirname is None:
            raise RuntimeError("Scenario_lp_mps_files extension"
                               " cannot be used without the"
                               " write_scenario_lp_mps_files_dir option")
        os.makedirs(self.dirname, exist_ok=False)


    def pre_iter0(self):
        dn = self.dirname  # typing aid
        for k, s in self.ph.local_subproblems.items():
            s.write(os.path.join(dn, f"{k}.lp"), io_options={'symbolic_solver_labels': True})
            s.write(os.path.join(dn, f"{k}.mps"), io_options={'symbolic_solver_labels': True})
            scenData = {"name": s.name, "scenProb": s._mpisppy_probability} 
            scenDict = {"scenarioData": scenData}
            treeData = dict()
            rhoList = list()
            for nd in s._mpisppy_node_list:
                treeData[nd.name] = {"condProb": nd.cond_prob}
                treeData[nd.name].update({"nonAnts": [lpize(var.name) for var in nd.nonant_vardata_list]})
                rhoList.extend((lpize(var.name), s._mpisppy_model.rho[(nd.name, i)]._value)\
                            for i,var in enumerate(nd.nonant_vardata_list))

            scenDict["treeData"] = treeData
            with open(os.path.join(dn, f"{k}_nonants.json"), "w") as jfile:
                json.dump(scenDict, jfile, indent=2)
            # Note that for two stage problems, all rho files will be the same
            with open(os.path.join(dn, f"{k}_rho.csv"), "w") as rfile:
                rfile.write("varname,rho\n")
                for name, rho in rhoList:
                    rfile.write(f"{name},{rho}\n")
                                        
    def post_iter0(self):
        return




