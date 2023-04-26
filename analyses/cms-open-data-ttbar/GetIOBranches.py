import json
import numpy as np

### OPTIONS
ratio_json_path = "nanoaod_branch_ratios.json"
agc_original_branches = ["Jet_pt", "Jet_eta", "Jet_phi", "Jet_btagCSVV2", "Jet_mass", 
                         "Muon_pt", "Electron_pt"]
desired_percents = [15,25,50]
config_json_path = "config.json"


def main():
    
    with open(ratio_json_path) as json_file:
        branch_ratios = json.load(json_file)
    
    io_branch_dict = {}

    # calculate percentage associated with original AGC branches
    current_sum = 0
    for key in branch_ratios.keys():
        if key in agc_original_branches:
            current_sum+=branch_ratios[key]
    io_branch_dict[np.round(100*current_sum,1)] = agc_original_branches

    sortind = np.argsort(list(branch_ratios.values()))
    keys = np.array(list(branch_ratios.keys()))[sortind]
    values = np.array(list(branch_ratios.values()))[sortind]

    for percent in desired_percents:
        branch_names = []
        current_sum = 0
        for i, key in enumerate(keys):
            branch_names.append(key)
            current_sum+=values[i]
            if 100*current_sum>=percent:
                print(f"Expected Percentage = {percent}, Calculated Percentage = {100*np.round(current_sum,3)}")
                break
        io_branch_dict[percent] = branch_names

    with open(config_json_path) as json_file:
        config = json.load(json_file)
    
    config["benchmarking"]["IO_BRANCHES"] = io_branch_dict
    
    with open(config_json_path, "w") as outfile:
        json.dump(config, outfile, indent=4)


if __name__ == "__main__":
    main()

