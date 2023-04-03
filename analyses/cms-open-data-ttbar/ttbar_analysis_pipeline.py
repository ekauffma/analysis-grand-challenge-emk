# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.14.1
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # CMS Open Data $t\bar{t}$: from data delivery to statistical inference
#
# We are using [2015 CMS Open Data](https://cms.cern/news/first-cms-open-data-lhc-run-2-released) in this demonstration to showcase an analysis pipeline.
# It features data delivery and processing, histogram construction and visualization, as well as statistical inference.
#
# This notebook was developed in the context of the [IRIS-HEP AGC tools 2022 workshop](https://indico.cern.ch/e/agc-tools-2).
# This work was supported by the U.S. National Science Foundation (NSF) Cooperative Agreement OAC-1836650 (IRIS-HEP).
#
# This is a **technical demonstration**.
# We are including the relevant workflow aspects that physicists need in their work, but we are not focusing on making every piece of the demonstration physically meaningful.
# This concerns in particular systematic uncertainties: we capture the workflow, but the actual implementations are more complex in practice.
# If you are interested in the physics side of analyzing top pair production, check out the latest results from [ATLAS](https://twiki.cern.ch/twiki/bin/view/AtlasPublic/TopPublicResults) and [CMS](https://cms-results.web.cern.ch/cms-results/public-results/preliminary-results/)!
# If you would like to see more technical demonstrations, also check out an [ATLAS Open Data example](https://indico.cern.ch/event/1076231/contributions/4560405/) demonstrated previously.
#
# This notebook implements most of the analysis pipeline shown in the following picture, using the tools also mentioned there:
# ![ecosystem visualization](utils/ecosystem.png)

# %% [markdown]
# ### Data pipelines
#
# There are two possible pipelines: one with `ServiceX` enabled, and one using only `coffea` for processing.
# ![processing pipelines](utils/processing_pipelines.png)

# %% [markdown]
# ### Imports: setting up our environment

# %%
import asyncio
import logging
import os
import time

import awkward as ak
import cabinetry
from coffea import processor
from coffea.nanoevents import NanoAODSchema
from servicex import ServiceXDataset
from func_adl import ObjectStream
from func_adl_servicex import ServiceXSourceUpROOT

import hist
import json
import matplotlib.pyplot as plt
import numpy as np
import uproot

# ML-related imports
from sklearn.preprocessing import PowerTransformer
import mlflow
from mlflow.models.signature import infer_signature
from mlflow.tracking import MlflowClient
from xgboost import XGBClassifier
from xgboost import plot_tree
import tritonclient.grpc as grpcclient
from sklearn.preprocessing import PowerTransformer

import utils  # contains code for bookkeeping and cosmetics, as well as some boilerplate

logging.getLogger("cabinetry").setLevel(logging.INFO)

# %% [markdown]
# ### Configuration: number of files and data delivery path
#
# The number of files per sample set here determines the size of the dataset we are processing.
# There are 9 samples being used here, all part of the 2015 CMS Open Data release.
# They are pre-converted from miniAOD files into ntuple format, similar to nanoAODs.
# More details about the inputs can be found [here](https://github.com/iris-hep/analysis-grand-challenge/tree/main/datasets/cms-open-data-2015).
#
# The table below summarizes the amount of data processed depending on the `N_FILES_MAX_PER_SAMPLE` setting.
#
# | setting | number of files | total size |
# | --- | --- | --- |
# | `1` | 12 | 25.1 GB |
# | `2` | 24 | 46.5 GB |
# | `5` | 52 | 110 GB |
# | `10` | 88 | 205 GB |
# | `20` | 149 | 364 GB |
# | `50` | 264 | 636 GB |
# | `100` | 404 | 965 GB |
# | `200` | 604 | 1.40 TB |
# | `-1` | 796 | 1.78 TB |
#
# The input files are all in the 1–3 GB range.

# %%
### GLOBAL CONFIGURATION

# input files per process, set to e.g. 10 (smaller number = faster)
N_FILES_MAX_PER_SAMPLE = 5

# enable Dask (currently will not work with Triton inference)
USE_DASK = True

# enable ServiceX
USE_SERVICEX = False

# ServiceX: ignore cache with repeated queries
SERVICEX_IGNORE_CACHE = False

# analysis facility: set to "coffea_casa" for coffea-casa environments, "EAF" for FNAL, "local" for local setups
AF = "coffea_casa"


### BENCHMARKING-SPECIFIC SETTINGS

# chunk size to use
CHUNKSIZE = 100_000

# metadata to propagate through to metrics
AF_NAME = "coffea_casa"  # "ssl-dev" allows for the switch to local data on /data
SYSTEMATICS = "all"  # currently has no effect
CORES_PER_WORKER = 2  # does not do anything, only used for metric gathering (set to 2 for distributed coffea-casa)

# scaling for local setups with FuturesExecutor
NUM_CORES = 8

# only I/O, all other processing disabled
DISABLE_PROCESSING = False

# read additional branches (only with DISABLE_PROCESSING = True)
# acceptable values are 2.7, 4, 15, 25, 50 (corresponding to % of file read), 2.7% corresponds to the standard branches used in the notebook
IO_FILE_PERCENT = 2.7

# maximum number of jets to consider for reconstruction BDT
MAX_N_JETS = 6

# whether to use NVIDIA Triton for inference (uses xgboost otherwise)
USE_TRITON = False

# path to local models (no triton)
XGBOOST_MODEL_PATH_EVEN = "models/model_230324_even.model"
XGBOOST_MODEL_PATH_ODD = "models/model_230324_odd.model"

# name of model loaded in triton server
MODEL_NAME = "reconstruction_bdt_xgb"

# versions of triton model to use
MODEL_VERSION_EVEN = "1"
MODEL_VERSION_ODD = "2"

# URL of triton server 
TRITON_URL = "agc-triton-inference-server:8001"

# %% [markdown]
# ### Machine Learning Task
#
# During the processing step, machine learning is used to calculate one of the variables used for this analysis. The models used are trained separately in the `jetassignment_training.ipynb` notebook. Jets in the events are assigned to labels corresponding with their parent partons using a boosted decision tree (BDT). More information about the model and training can be found within that notebook. To obtain the features used as inputs for the BDT, we use the methods defined below:

# %%
permutations_dict = utils.get_permutations_dict(MAX_N_JETS)


# %%
def get_features(jets, electrons, muons, permutations_dict):
    
    '''
    Calculate features for each of the combinations per event and calculates combination-level labels
    
    Args:
        jets: selected jets after training filter
        electrons: selected electrons after training filter
        muons: selected muons after training filter
        permutations_dict: dictionary containing the permutation indices for each number of jets
    
    Returns:
        features (flattened to remove event level)
    '''
    
    # calculate number of jets in each event
    njet = ak.num(jets).to_numpy()
    # don't consider every jet for events with high jet multiplicity
    njet[njet>max(permutations_dict.keys())] = max(permutations_dict.keys())
    # create awkward array of permutation indices
    perms = ak.Array([permutations_dict[n] for n in njet])
    perm_counts = ak.num(perms)
    
    #### calculate features ####
    features = np.zeros((sum(perm_counts),28))
    
    # grab lepton info
    leptons = ak.flatten(ak.concatenate((electrons, muons),axis=1),axis=-1)

    feature_count = 0
    
    # delta R between top_lepton and lepton
    features[:,0] = ak.flatten(np.sqrt((leptons.eta - jets[perms[...,3]].eta)**2 + 
                                       (leptons.phi - jets[perms[...,3]].phi)**2)).to_numpy()

    
    #delta R between the two W
    features[:,1] = ak.flatten(np.sqrt((jets[perms[...,0]].eta - jets[perms[...,1]].eta)**2 + 
                                       (jets[perms[...,0]].phi - jets[perms[...,1]].phi)**2)).to_numpy()

    #delta R between W and top_hadron
    features[:,2] = ak.flatten(np.sqrt((jets[perms[...,0]].eta - jets[perms[...,2]].eta)**2 + 
                                       (jets[perms[...,0]].phi - jets[perms[...,2]].phi)**2)).to_numpy()
    features[:,3] = ak.flatten(np.sqrt((jets[perms[...,1]].eta - jets[perms[...,2]].eta)**2 + 
                                       (jets[perms[...,1]].phi - jets[perms[...,2]].phi)**2)).to_numpy()

    # delta phi between top_lepton and lepton
    features[:,4] = ak.flatten(np.abs(leptons.phi - jets[perms[...,3]].phi)).to_numpy()

    # delta phi between the two W
    features[:,5] = ak.flatten(np.abs(jets[perms[...,0]].phi - jets[perms[...,1]].phi)).to_numpy()

    # delta phi between W and top_hadron
    features[:,6] = ak.flatten(np.abs(jets[perms[...,0]].phi - jets[perms[...,2]].phi)).to_numpy()
    features[:,7] = ak.flatten(np.abs(jets[perms[...,1]].phi - jets[perms[...,2]].phi)).to_numpy()

    # combined mass of top_lepton and lepton
    features[:,8] = ak.flatten((leptons + jets[perms[...,3]]).mass).to_numpy()

    # combined mass of W
    features[:,9] = ak.flatten((jets[perms[...,0]] + jets[perms[...,1]]).mass).to_numpy()

    # combined mass of W and top_hadron
    features[:,10] = ak.flatten((jets[perms[...,0]] + jets[perms[...,1]] + 
                                 jets[perms[...,2]]).mass).to_numpy()
    
    feature_count+=1
    # combined pT of W and top_hadron
    features[:,11] = ak.flatten((jets[perms[...,0]] + jets[perms[...,1]] + 
                                 jets[perms[...,2]]).pt).to_numpy()


    # pt of every jet
    features[:,12] = ak.flatten(jets[perms[...,0]].pt).to_numpy()
    features[:,13] = ak.flatten(jets[perms[...,1]].pt).to_numpy()
    features[:,14] = ak.flatten(jets[perms[...,2]].pt).to_numpy()
    features[:,15] = ak.flatten(jets[perms[...,3]].pt).to_numpy()

    # mass of every jet
    features[:,16] = ak.flatten(jets[perms[...,0]].mass).to_numpy()
    features[:,17] = ak.flatten(jets[perms[...,1]].mass).to_numpy()
    features[:,18] = ak.flatten(jets[perms[...,2]].mass).to_numpy()
    features[:,19] = ak.flatten(jets[perms[...,3]].mass).to_numpy()
    
    # btagCSVV2 of every jet
    features[:,20] = ak.flatten(jets[perms[...,0]].btagCSVV2).to_numpy()
    features[:,21] = ak.flatten(jets[perms[...,1]].btagCSVV2).to_numpy()
    features[:,22] = ak.flatten(jets[perms[...,2]].btagCSVV2).to_numpy()
    features[:,23] = ak.flatten(jets[perms[...,3]].btagCSVV2).to_numpy()
    
    # qgl of every jet
    features[:,24] = ak.flatten(jets[perms[...,0]].qgl).to_numpy()
    features[:,25] = ak.flatten(jets[perms[...,1]].qgl).to_numpy()
    features[:,26] = ak.flatten(jets[perms[...,2]].qgl).to_numpy()
    features[:,27] = ak.flatten(jets[perms[...,3]].qgl).to_numpy()
    
    return features.astype(np.float32), perm_counts


# %% [markdown]
# ### Defining our `coffea` Processor
#
# The processor includes a lot of the physics analysis details:
# - event filtering and the calculation of observables,
# - event weighting,
# - calculating systematic uncertainties at the event and object level,
# - filling all the information into histograms that get aggregated and ultimately returned to us by `coffea`.

# %% tags=[]
# functions creating systematic variations
def flat_variation(ones):
    # 2.5% weight variations
    return (1.0 + np.array([0.025, -0.025], dtype=np.float32)) * ones[:, None]


def btag_weight_variation(i_jet, jet_pt):
    # weight variation depending on i-th jet pT (7.5% as default value, multiplied by i-th jet pT / 50 GeV)
    return 1 + np.array([0.075, -0.075]) * (ak.singletons(jet_pt[:, i_jet]) / 50).to_numpy()


def jet_pt_resolution(pt):
    # normal distribution with 5% variations, shape matches jets
    counts = ak.num(pt)
    pt_flat = ak.flatten(pt)
    resolution_variation = np.random.normal(np.ones_like(pt_flat), 0.05)
    return ak.unflatten(resolution_variation, counts)


class TtbarAnalysis(processor.ProcessorABC):
    def __init__(self, disable_processing, io_file_percent, use_triton, xgboost_model_even, xgboost_model_odd, 
                 model_name, model_vers_even, model_vers_odd, url, permutations_dict):
        
        # initialize histogram
        num_bins = 25
        bin_low = 50
        bin_high = 550
        name = "observable"
        label = "observable [GeV]"
        self.hist = (
            hist.Hist.new.Reg(num_bins, bin_low, bin_high, name=name, label=label)
            .Reg(num_bins, bin_low, bin_high, name="ml_observable", label="ML observable [GeV]")
            .StrCat(["4j1b", "4j2b"], name="region", label="Region")
            .StrCat([], name="process", label="Process", growth=True)
            .StrCat([], name="variation", label="Systematic variation", growth=True)
            .Weight()
        )
        self.disable_processing = disable_processing
        self.io_file_percent = io_file_percent
        
        # for ML inference
        self.use_triton = use_triton
        self.xgboost_model_even = xgboost_model_even
        self.xgboost_model_odd = xgboost_model_odd
        self.model_name = model_name
        self.model_vers_even = model_vers_even
        self.model_vers_odd = model_vers_odd
        self.url = url
        self.permutations_dict = permutations_dict

    def only_do_IO(self, events):
        # standard AGC branches cover 2.7% of the data
            branches_to_read = []
            if self.io_file_percent >= 2.7:
                branches_to_read.extend(["Jet_pt", "Jet_eta", "Jet_phi", "Jet_btagCSVV2", "Jet_mass", "Muon_pt", "Electron_pt"])
            
            if self.io_file_percent >= 4:
                branches_to_read.extend(["Electron_phi", "Electron_eta","Electron_mass","Muon_phi","Muon_eta","Muon_mass",
                                         "Photon_pt","Photon_eta","Photon_mass","Jet_jetId"])
            
            if self.io_file_percent>=15:
                branches_to_read.extend(["Jet_nConstituents","Jet_electronIdx1","Jet_electronIdx2","Jet_muonIdx1","Jet_muonIdx2",
                                         "Jet_chHEF","Jet_area","Jet_puId","Jet_qgl","Jet_btagDeepB","Jet_btagDeepCvB",
                                         "Jet_btagDeepCvL","Jet_btagDeepFlavB","Jet_btagDeepFlavCvB","Jet_btagDeepFlavCvL",
                                         "Jet_btagDeepFlavQG","Jet_chEmEF","Jet_chFPV0EF","Jet_muEF","Jet_muonSubtrFactor",
                                         "Jet_neEmEF","Jet_neHEF","Jet_puIdDisc"])
            
            if self.io_file_percent>=25:
                branches_to_read.extend(["GenPart_pt","GenPart_eta","GenPart_phi","GenPart_mass","GenPart_genPartIdxMother",
                                         "GenPart_pdgId","GenPart_status","GenPart_statusFlags"])
            
            if self.io_file_percent==50:
                branches_to_read.extend(["Jet_rawFactor","Jet_bRegCorr","Jet_bRegRes","Jet_cRegCorr","Jet_cRegRes","Jet_nElectrons",
                                         "Jet_nMuons","GenJet_pt","GenJet_eta","GenJet_phi","GenJet_mass","Tau_pt","Tau_eta","Tau_mass",
                                         "Tau_phi","Muon_dxy","Muon_dxyErr","Muon_dxybs","Muon_dz","Muon_dzErr","Electron_dxy",
                                         "Electron_dxyErr","Electron_dz","Electron_dzErr","Electron_eInvMinusPInv","Electron_energyErr",
                                         "Electron_hoe","Electron_ip3d","Electron_jetPtRelv2","Electron_jetRelIso",
                                         "Electron_miniPFRelIso_all","Electron_miniPFRelIso_chg","Electron_mvaFall17V2Iso",
                                         "Electron_mvaFall17V2noIso","Electron_pfRelIso03_all","Electron_pfRelIso03_chg","Electron_r9",
                                         "Electron_scEtOverPt","Electron_sieie","Electron_sip3d","Electron_mvaTTH","Electron_charge",
                                         "Electron_cutBased","Electron_jetIdx","Electron_pdgId","Electron_photonIdx","Electron_tightCharge"])
                
            if self.io_file_percent not in [2.7, 4, 15, 25, 50]:
                raise NotImplementedError("supported values for I/O percentage are 2.7, 4, 15, 25, 50")
            
            for branch in branches_to_read:
                if "_" in branch:
                    split = branch.split("_")
                    object_type = split[0]
                    property_name = '_'.join(split[1:])
                    ak.materialized(events[object_type][property_name])
                else:
                    ak.materialized(events[branch])
            return {"hist": {}}

    def process(self, events):
        if self.disable_processing:
            # IO testing with no subsequent processing
            return self.only_do_IO(events)

        histogram = self.hist.copy()

        process = events.metadata["process"]  # "ttbar" etc.
        variation = events.metadata["variation"]  # "nominal" etc.

        # normalization for MC
        x_sec = events.metadata["xsec"]
        nevts_total = events.metadata["nevts"]
        lumi = 3378 # /pb
        if process != "data":
            xsec_weight = x_sec * lumi / nevts_total
        else:
            xsec_weight = 1
            
        if self.use_triton:
            # setup triton gRPC client
            triton_client = grpcclient.InferenceServerClient(url=self.url)
            model_metadata = triton_client.get_model_metadata(self.model_name, self.model_vers)
            input_name = model_metadata.inputs[0].name
            dtype = model_metadata.inputs[0].datatype
            output_name = model_metadata.outputs[0].name
        

        #### systematics
        #example of a simple flat weight variation, using the coffea nanoevents systematics feature
        if process == "wjets":
            events.add_systematic("scale_var", "UpDownSystematic", "weight", flat_variation)

        #jet energy scale / resolution systematics
        #need to adjust schema to instead use coffea add_systematic feature, especially for ServiceX
        #cannot attach pT variations to events.jet, so attach to events directly
        #and subsequently scale pT by these scale factors
        events["pt_nominal"] = 1.0
        events["pt_scale_up"] = 1.03
        events["pt_res_up"] = jet_pt_resolution(events.Jet.pt)

        pt_variations = ["pt_nominal", "pt_scale_up", "pt_res_up"] if variation == "nominal" else ["pt_nominal"]
        for pt_var in pt_variations:

            ### event selection
            # very very loosely based on https://arxiv.org/abs/2006.13076

            # pT > 25 GeV for leptons & jets
            selected_electrons = events.Electron[(events.Electron.pt>25)]
            selected_muons = events.Muon[(events.Muon.pt >25)]
            jet_filter = (events.Jet.pt * events[pt_var]) > 25
            selected_jets = events.Jet[jet_filter]
            even = (events.event%2==0)

            # single lepton requirement
            event_filters = ((ak.count(selected_electrons.pt, axis=1) + ak.count(selected_muons.pt, axis=1)) == 1)
            # at least four jets
            pt_var_modifier = events[pt_var] if "res" not in pt_var else events[pt_var][jet_filter]
            event_filters = event_filters & (ak.count(selected_jets.pt * pt_var_modifier, axis=1) >= 4)
            # at least one b-tagged jet ("tag" means score above threshold)
            B_TAG_THRESHOLD = 0.5
            event_filters = event_filters & (ak.sum(selected_jets.btagCSVV2 >= B_TAG_THRESHOLD, axis=1) >= 1)

            # apply event filters
            selected_events = events[event_filters]
            selected_electrons = selected_electrons[event_filters]
            selected_muons = selected_muons[event_filters]
            selected_jets = selected_jets[event_filters]
            even = even[event_filters]

            for region in ["4j1b", "4j2b"]:
                # further filtering: 4j1b CR with single b-tag, 4j2b SR with two or more tags
                if region == "4j1b":
                    region_filter = ak.sum(selected_jets.btagCSVV2 >= B_TAG_THRESHOLD, axis=1) == 1
                    selected_jets_region = selected_jets[region_filter]
                    even_region = even[region_filter]
                    
                    # use HT (scalar sum of jet pT) as observable
                    pt_var_modifier = (
                        events[event_filters][region_filter][pt_var]
                        if "res" not in pt_var
                        else events[pt_var][jet_filter][event_filters][region_filter]
                    )
                    observable = ak.sum(selected_jets_region.pt * pt_var_modifier, axis=-1)
                    ML_observable = observable

                elif region == "4j2b":
                    region_filter = ak.sum(selected_jets.btagCSVV2 > B_TAG_THRESHOLD, axis=1) >= 2
                    selected_jets_region = selected_jets[region_filter]
                    selected_electrons_region = selected_electrons[region_filter]
                    selected_muons_region = selected_muons[region_filter]
                    even_region = even[region_filter]

                    # reconstruct hadronic top as bjj system with largest pT
                    # the jet energy scale / resolution effect is not propagated to this observable at the moment
                    trijet = ak.combinations(selected_jets_region, 3, fields=["j1", "j2", "j3"])  # trijet candidates
                    trijet["p4"] = trijet.j1 + trijet.j2 + trijet.j3  # calculate four-momentum of tri-jet system
                    trijet["max_btag"] = np.maximum(trijet.j1.btagCSVV2, np.maximum(trijet.j2.btagCSVV2, trijet.j3.btagCSVV2))
                    trijet = trijet[trijet.max_btag > B_TAG_THRESHOLD]  # at least one-btag in trijet candidates
                    # pick trijet candidate with largest pT and calculate mass of system
                    trijet_mass = trijet["p4"][ak.argmax(trijet.p4.pt, axis=1, keepdims=True)].mass
                    observable = ak.flatten(trijet_mass)
                    
                    # get ml features
                    features, perm_counts = get_features(selected_jets_region, selected_electrons_region, 
                                                         selected_muons_region, self.permutations_dict)
                    even_perm = np.repeat(even_region, perm_counts)
                    power = PowerTransformer(method='yeo-johnson', standardize=True)
                    
                    #calculate ml observable
                    if self.use_triton:
                        
                        results = np.zeros(features.shape[0])
                        output = grpcclient.InferRequestedOutput(output_name)
                        
                        inpt = [grpcclient.InferInput(input_name, features[even_perm].shape, dtype)]
                        inpt[0].set_data_from_numpy(power.fit_transform(features[even_perm]))
                        results[even_region]=triton_client.infer(
                            model_name=self.model_name, 
                            model_version=self.model_version_even,
                            inputs=inpt, 
                            outputs=[output]
                        ).as_numpy(output_name)[:, 1]
                        
                        inpt = [grpcclient.InferInput(input_name, features[np.invert(even_perm)].shape, dtype)]
                        inpt[0].set_data_from_numpy(power.fit_transform(features[np.invert(even_perm)]))
                        results[np.invert(even_region)]=triton_client.infer(
                            model_name=self.model_name, 
                            model_version=self.model_version_odd,
                            inputs=inpt, 
                            outputs=[output]
                        ).as_numpy(output_name)[:, 1]
                    
                    else:
                        
                        results = np.zeros(features.shape[0])
                        results[even_perm] = self.xgboost_model_even.predict_proba(
                            power.fit_transform(features[even_perm,:])
                        )[:, 1]
                        results[np.invert(even_perm)] = results_odd = self.xgboost_model_odd.predict_proba(
                            power.fit_transform(features[np.invert(even_perm),:])
                        )[:, 1]
                        
                    results = ak.unflatten(results, perm_counts)
                    which_combination = ak.argmax(results,axis=1)
                    features_unflattened = ak.unflatten(features, perm_counts)
                    ML_observable = ak.flatten(features_unflattened[ak.from_regular(which_combination[:, np.newaxis])])[...,10]
                    
                ### histogram filling
                if pt_var == "pt_nominal":
                    # nominal pT, but including 2-point systematics
                    histogram.fill(
                            observable=observable, ml_observable=ML_observable, region=region, process=process,
                            variation=variation, weight=xsec_weight
                        )

                    if variation == "nominal":
                        # also fill weight-based variations for all nominal samples
                        for weight_name in events.systematics.fields:
                            for direction in ["up", "down"]:
                                # extract the weight variations and apply all event & region filters
                                weight_variation = events.systematics[weight_name][direction][
                                    f"weight_{weight_name}"][event_filters][region_filter]
                                # fill histograms
                                histogram.fill(
                                    observable=observable, ml_observable=ML_observable, region=region, process=process,
                                    variation=f"{weight_name}_{direction}", weight=xsec_weight*weight_variation
                                )

                        # calculate additional systematics: b-tagging variations
                        for i_var, weight_name in enumerate([f"btag_var_{i}" for i in range(4)]):
                            for i_dir, direction in enumerate(["up", "down"]):
                                # create systematic variations that depend on object properties (here: jet pT)
                                if len(observable):
                                    weight_variation = btag_weight_variation(i_var, selected_jets_region.pt)[:, i_dir]
                                else:
                                    weight_variation = 1 # no events selected
                                histogram.fill(
                                    observable=observable, ml_observable=ML_observable, region=region, process=process,
                                    variation=f"{weight_name}_{direction}", weight=xsec_weight*weight_variation
                                )

                elif variation == "nominal":
                    # pT variations for nominal samples
                    histogram.fill(
                            observable=observable, ml_observable=ML_observable, region=region, process=process,
                            variation=pt_var, weight=xsec_weight
                        )

        
        output = {"nevents": {events.metadata["dataset"]: len(events)},
                  "training_entries": {events.metadata["dataset"]: len(features)},
                  "nevents_reduced": {events.metadata["dataset"]: len(observable)},
                  "hist": histogram}
        
        if self.use_triton:
            triton_client.close()
            
        return output

    def postprocess(self, accumulator):
        return accumulator

# %% [markdown]
# ### "Fileset" construction and metadata
#
# Here, we gather all the required information about the files we want to process: paths to the files and asociated metadata.

# %% tags=[]
fileset = utils.construct_fileset(N_FILES_MAX_PER_SAMPLE, use_xcache=False, af_name=AF_NAME)  # local files on /data for ssl-dev

print(f"processes in fileset: {list(fileset.keys())}")
print(f"\nexample of information in fileset:\n{{\n  'files': [{fileset['ttbar__nominal']['files'][0]}, ...],")
print(f"  'metadata': {fileset['ttbar__nominal']['metadata']}\n}}")


# %% [markdown]
# ### ServiceX-specific functionality: query setup
#
# Define the func_adl query to be used for the purpose of extracting columns and filtering.

# %%
def get_query(source: ObjectStream) -> ObjectStream:
    """Query for event / column selection: >=4j >=1b, ==1 lep with pT>25 GeV, return relevant columns
    """
    return source.Where(lambda e: e.Electron_pt.Where(lambda pt: pt > 25).Count() 
                        + e.Muon_pt.Where(lambda pt: pt > 25).Count() == 1)\
                 .Where(lambda e: e.Jet_pt.Where(lambda pt: pt > 25).Count() >= 4)\
                 .Where(lambda g: {"pt": g.Jet_pt, 
                                   "btagCSVV2": g.Jet_btagCSVV2}.Zip().Where(lambda jet: 
                                                                             jet.btagCSVV2 >= 0.5 
                                                                             and jet.pt > 25).Count() >= 1)\
                 .Select(lambda f: {"Electron_pt": f.Electron_pt,
                                    "Muon_pt": f.Muon_pt,
                                    "Jet_mass": f.Jet_mass,
                                    "Jet_pt": f.Jet_pt,
                                    "Jet_eta": f.Jet_eta,
                                    "Jet_phi": f.Jet_phi,
                                    "Jet_btagCSVV2": f.Jet_btagCSVV2,
                                   })


# %% [markdown]
# ### Caching the queried datasets with `ServiceX`
#
# Using the queries created with `func_adl`, we are using `ServiceX` to read the CMS Open Data files to build cached files with only the specific event information as dictated by the query.

# %%
if USE_SERVICEX:
    
    # dummy dataset on which to generate the query
    dummy_ds = ServiceXSourceUpROOT("cernopendata://dummy", "Events", backend_name="uproot")

    # tell low-level infrastructure not to contact ServiceX yet, only to
    # return the qastle string it would have sent
    dummy_ds.return_qastle = True

    # create the query
    query = get_query(dummy_ds).value()

    # now we query the files and create a fileset dictionary containing the
    # URLs pointing to the queried files

    t0 = time.time()
    for process in fileset.keys():
        ds = ServiceXDataset(fileset[process]['files'], 
                             backend_name="uproot", 
                             ignore_cache=SERVICEX_IGNORE_CACHE)
        files = ds.get_data_rootfiles_uri(query, 
                                          as_signed_url=True,
                                          title=process)

        
        fileset[process]["files"] = [f.url for f in files]

    print(f"ServiceX data delivery took {time.time() - t0:.2f} seconds")

# %% [markdown]
# ### Execute the data delivery pipeline
#
# What happens here depends on the flag `USE_SERVICEX`. If set to true, the processor is run on the data previously gathered by ServiceX, then will gather output histograms.
#
# When `USE_SERVICEX` is false, the input files need to be processed during this step as well.

# %%
NanoAODSchema.warn_missing_crossrefs = False # silences warnings about branches we will not use here
if USE_DASK:
    executor = processor.DaskExecutor(client=utils.get_client(AF))
else:
    executor = processor.FuturesExecutor(workers=NUM_CORES)
        
run = processor.Runner(executor=executor, schema=NanoAODSchema, savemetrics=True, 
                       metadata_cache={}, chunksize=CHUNKSIZE)#, maxchunks=1)

if USE_SERVICEX:
    treename = "servicex"
    
else:
    treename = "Events"
    
filemeta = run.preprocess(fileset, treename=treename)  # pre-processing

if not USE_TRITON:
    model_even = XGBClassifier()
    model_even.load_model(XGBOOST_MODEL_PATH_EVEN)
    model_odd = XGBClassifier()
    model_odd.load_model(XGBOOST_MODEL_PATH_ODD)
    
else:
    model_even = None
    model_odd = None

t0 = time.monotonic()
# processing
all_histograms, metrics = run(fileset, treename, processor_instance=TtbarAnalysis(DISABLE_PROCESSING, IO_FILE_PERCENT, 
                                                                                  USE_TRITON, model_even, model_odd,
                                                                                  MODEL_NAME, MODEL_VERSION_EVEN, MODEL_VERSION_ODD, 
                                                                                  TRITON_URL, permutations_dict)) 
exec_time = time.monotonic() - t0

# all_histograms = all_histograms["hist"]

print(f"\nexecution took {exec_time:.2f} seconds")

# %%
# track metrics
dataset_source = "/data" if fileset["ttbar__nominal"]["files"][0].startswith("/data") else "https://xrootd-local.unl.edu:1094" # TODO: xcache support
metrics.update({
    "walltime": exec_time, 
    "num_workers": NUM_CORES, 
    "af": AF_NAME, 
    "dataset_source": dataset_source, 
    "use_dask": USE_DASK, 
    "use_servicex": USE_SERVICEX, 
    "systematics": SYSTEMATICS, 
    "n_files_max_per_sample": N_FILES_MAX_PER_SAMPLE,
    "cores_per_worker": CORES_PER_WORKER, 
    "chunksize": CHUNKSIZE, 
    "disable_processing": DISABLE_PROCESSING, 
    "io_file_percent": IO_FILE_PERCENT
})

# save metrics to disk
if not os.path.exists("metrics"):
    os.makedirs("metrics")
timestamp = time.strftime('%Y%m%d-%H%M%S')
metric_file_name = f"metrics/{AF_NAME}-{timestamp}.json"
with open(metric_file_name, "w") as f:
    f.write(json.dumps(metrics))

print(f"metrics saved as {metric_file_name}")
#print(f"event rate per worker (full execution time divided by NUM_CORES={NUM_CORES}): {metrics['entries'] / NUM_CORES / exec_time / 1_000:.2f} kHz")
print(f"event rate per worker (pure processtime): {metrics['entries'] / metrics['processtime'] / 1_000:.2f} kHz")
print(f"amount of data read: {metrics['bytesread']/1000**2:.2f} MB")  # likely buggy: https://github.com/CoffeaTeam/coffea/issues/717

# %% [markdown]
# ### Inspecting the produced histograms
#
# Let's have a look at the data we obtained.
# We built histograms in two phase space regions, for multiple physics processes and systematic variations.

# %%
utils.set_style()

all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", :, "nominal"].stack("process").project("observable").plot(stack=True, histtype="fill", linewidth=1, edgecolor="grey")
plt.legend(frameon=False)
plt.title(">= 4 jets, 1 b-tag")
plt.xlabel("HT [GeV]");

# %%
all_histograms["hist"][:, 120j::hist.rebin(2), "4j1b", :, "nominal"].stack("process").project("ml_observable").plot(stack=True, histtype="fill", linewidth=1, edgecolor="grey")
plt.legend(frameon=False)
plt.title(">= 4 jets, 1 b-tag")
plt.xlabel("HT [GeV]");

# %%
all_histograms["hist"][:, :, "4j2b", :, "nominal"].stack("process").project("observable").plot(stack=True, histtype="fill", linewidth=1, edgecolor="grey")
plt.legend(frameon=False)
plt.title(">= 4 jets, >= 2 b-tags")
plt.xlabel("$m_{bjj}$ [Gev]");

# %%
all_histograms["hist"][:, :, "4j2b", :, "nominal"].stack("process").project("ml_observable").plot(stack=True, histtype="fill", linewidth=1, edgecolor="grey")
plt.legend(frameon=False)
plt.title(">= 4 jets, >= 2 b-tags")
plt.xlabel("$m_{bjj}$ [Gev]");

# %% [markdown]
# Our top reconstruction approach ($bjj$ system with largest $p_T$) has worked!
#
# Let's also have a look at some systematic variations:
# - b-tagging, which we implemented as jet-kinematic dependent event weights,
# - jet energy variations, which vary jet kinematics, resulting in acceptance effects and observable changes.
#
# We are making of [UHI](https://uhi.readthedocs.io/) here to re-bin.

# %%
# b-tagging variations
all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", "ttbar", "nominal"].project("observable").plot(label="nominal", linewidth=2)
all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", "ttbar", "btag_var_0_up"].project("observable").plot(label="NP 1", linewidth=2)
all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", "ttbar", "btag_var_1_up"].project("observable").plot(label="NP 2", linewidth=2)
all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", "ttbar", "btag_var_2_up"].project("observable").plot(label="NP 3", linewidth=2)
all_histograms["hist"][120j::hist.rebin(2), :, "4j1b", "ttbar", "btag_var_3_up"].project("observable").plot(label="NP 4", linewidth=2)
plt.legend(frameon=False)
plt.xlabel("HT [GeV]")
plt.title("b-tagging variations");

# %%
# jet energy scale variations
all_histograms["hist"][:, :, "4j2b", "ttbar", "nominal"].project("observable").plot(label="nominal", linewidth=2)
all_histograms["hist"][:, :, "4j2b", "ttbar", "pt_scale_up"].project("observable").plot(label="scale up", linewidth=2)
all_histograms["hist"][:, :, "4j2b", "ttbar", "pt_res_up"].project("observable").plot(label="resolution up", linewidth=2)
plt.legend(frameon=False)
plt.xlabel("$m_{bjj}$ [Gev]")
plt.title("Jet energy variations (Trijet combination method)");
plt.show()

all_histograms["hist"][:, :, "4j2b", "ttbar", "nominal"].project("ml_observable").plot(label="nominal", linewidth=2)
all_histograms["hist"][:, :, "4j2b", "ttbar", "pt_scale_up"].project("ml_observable").plot(label="scale up", linewidth=2)
all_histograms["hist"][:, :, "4j2b", "ttbar", "pt_res_up"].project("ml_observable").plot(label="resolution up", linewidth=2)
plt.legend(frameon=False)
plt.xlabel("$m_{bjj}$ [Gev]")
plt.title("Jet energy variations (BDT method)");
plt.show()

# %% [markdown]
# ### Save histograms to disk
#
# We'll save everything to disk for subsequent usage.
# This also builds pseudo-data by combining events from the various simulation setups we have processed.

# %%
utils.save_histograms(all_histograms["hist"], fileset, "histograms_noml.root", ml=False)
utils.save_histograms(all_histograms["hist"], fileset, "histograms_ml.root", ml=True)

# %% [markdown]
# ### Statistical inference
#
# A statistical model has been defined in `config.yml`, ready to be used with our output.
# We will use `cabinetry` to combine all histograms into a `pyhf` workspace and fit the resulting statistical model to the pseudodata we built.

# %%
# no ml version
config = cabinetry.configuration.load("cabinetry_config.yml")
cabinetry.templates.collect(config)
cabinetry.templates.postprocess(config)  # optional post-processing (e.g. smoothing)
ws = cabinetry.workspace.build(config)
cabinetry.workspace.save(ws, "workspace.json")

# %% [markdown]
# We can inspect the workspace with `pyhf`, or use `pyhf` to perform inference.

# %%
# !pyhf inspect workspace.json | head -n 20

# %% [markdown]
# Let's try out what we built: the next cell will perform a maximum likelihood fit of our statistical model to the pseudodata we built.

# %%
model, data = cabinetry.model_utils.model_and_data(ws)
fit_results = cabinetry.fit.fit(model, data)

cabinetry.visualize.pulls(
    fit_results, exclude="ttbar_norm", close_figure=True, save_figure=False
)

# %% [markdown]
# For this pseudodata, what is the resulting ttbar cross-section divided by the Standard Model prediction?

# %%
poi_index = model.config.poi_index
print(f"\nfit result for ttbar_norm: {fit_results.bestfit[poi_index]:.3f} +/- {fit_results.uncertainty[poi_index]:.3f}")

# %% [markdown]
# Let's also visualize the model before and after the fit, in both the regions we are using.
# The binning here corresponds to the binning used for the fit.

# %%
model_prediction = cabinetry.model_utils.prediction(model)
figs = cabinetry.visualize.data_mc(model_prediction, data, close_figure=True)
figs[0]["figure"]

# %%
figs[1]["figure"]

# %% [markdown]
# We can see very good post-fit agreement.

# %%
model_prediction_postfit = cabinetry.model_utils.prediction(model, fit_results=fit_results)
figs = cabinetry.visualize.data_mc(model_prediction_postfit, data, close_figure=True)
figs[0]["figure"]

# %%
figs[1]["figure"]

# %% [markdown]
# ### ML Validation
# We used two methods to reconstruct the top mass: choosing the three-jet system with the highest $p_T$ and choosing the three jets based on the output from out boosted decision tree. We can further validate our results by applying the above fit to the ML variable and checking for good agreement.

# %%
# load the ml workspace (uses the ml observable instead of previous method)
config_ml = cabinetry.configuration.load("cabinetry_config_ml.yml")
cabinetry.templates.collect(config_ml)
cabinetry.templates.postprocess(config_ml)  # optional post-processing (e.g. smoothing)
ws_ml = cabinetry.workspace.build(config_ml)
cabinetry.workspace.save(ws_ml, "workspace_ml.json")

# %%
model_ml, data_ml = cabinetry.model_utils.model_and_data(ws_ml)

# %% [markdown]
# Let's view the model obtained using the ML observable before the fit:

# %%
model_prediction = cabinetry.model_utils.prediction(model_ml)
figs = cabinetry.visualize.data_mc(model_prediction, data_ml, close_figure=True)
figs[1]["figure"]

# %% [markdown]
# Now applying the fit results from the trijet combination observable to the ML observable, let's see whether we have good data-MC agreement:

# %%
model_prediction_postfit = cabinetry.model_utils.prediction(model_ml, fit_results=fit_results)
figs = cabinetry.visualize.data_mc(model_prediction_postfit, data_ml, close_figure=True)
figs[1]["figure"]

# %% [markdown]
# We still see very good post-fit agreement here.

# %% [markdown]
# ### What is next?
#
# Our next goals for this pipeline demonstration are:
# - making this analysis even **more feature-complete**,
# - **addressing performance bottlenecks** revealed by this demonstrator,
# - **collaborating** with you!
#
# Please do not hesitate to get in touch if you would like to join the effort, or are interested in re-implementing (pieces of) the pipeline with different tools!
#
# Our mailing list is analysis-grand-challenge@iris-hep.org, sign up via the [Google group](https://groups.google.com/a/iris-hep.org/g/analysis-grand-challenge).
