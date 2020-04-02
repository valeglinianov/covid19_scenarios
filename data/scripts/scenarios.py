import sys
import csv
import os
import json
import numpy as np
import multiprocessing as multi

sys.path.append('..')

from datetime import datetime
from scipy.stats import linregress
from paths import TMP_CASES, BASE_PATH, JSON_DIR
from scripts.tsv import parse as parse_tsv
from scripts.model import fit_population

# ------------------------------------------------------------------------
# Globals

SCENARIO_POPS = os.path.join(BASE_PATH, "populationData.tsv")
FIT_CASE_DATA = {}

# ------------------------------------------------------------------------
# Dynamically create Python classes that match input parameter structure of app

# TODO: Fix the path to not be hard-coded
DEFAULTS   = {}
import schemapi
with open("../src/assets/types/params.schema.json") as fd:
    SCHEMA_DB  = json.load(fd)
    SCHEMA_API = schemapi.SchemaModuleGenerator(SCHEMA_DB, root_name="AllParams")
    SCHEMA     = SCHEMA_API.import_as("SCHEMA")
    # Log default value annotations
    for kind, param in SCHEMA_DB['definitions'].items():
        DEFAULTS[kind] = {}
        for prop, entry in param['properties'].items():
            if 'default' in entry:
                DEFAULTS[kind][prop] = entry['default']

from SCHEMA import PopulationParams, EpidemiologicalParams, DateRange, SimulationParams, ContainmentParams, AllParams

# ------------------------------------------------------------------------
# Wrappers for parameter class constructors

class Object:
    def marshalJSON(self):
        return json.dumps(self, default=lambda x: x.__dict__, sort_keys=True, indent=4)

class Measure(Object):
    def __init__(self, name, tMin, tMax, value):
        self.name = name
        self.timeRange = DateRange(tMin, tMax)
        self.mitigationValue = value

def make_population_params(region, country, population, beds, icus):
    return PopulationParams(
                populationServed    = int(population),
                country             = country,
                suspectedCasesToday = FIT_CASE_DATA[region]['initialCases'] if region in FIT_CASE_DATA else Fitter.cases_on_tMin,
                importsPerDay       = round(max(3e-4 * float(population)**0.5, .1),1),
                hospitalBeds        = int(beds),
                ICUBeds             = int(icus),
                cases               = region,
                **DEFAULTS['PopulationParams'])

def make_epidemiological_params(region, hemisphere):
    seasonalForcing = 2.0
    peakMonth       = 0
    if hemisphere:
        if hemisphere == "Northern":
            seasonalForcing = 0.2
            peakMonth       = 0
        elif hemisphere == "Southern":
            seasonalForcing = 0.2
            peakMonth       = 6
        elif hemisphere == "Tropical":
            seasonalForcing = 0.0
            peakMonth       = 0
        else:
            raise ValueError(f"Could not parse hemisphere for region '{region}'")

    r0 = 2.7
    if region in FIT_CASE_DATA:
        self.r0 = round(FIT_CASE_DATA[region]['r0'],1)

    return EpidemiologicalParams(r0=r0, seasonalForcing=seasonalForcing, peakMonth=peakMonth, **DEFAULTS['EpidemiologicalParams'])

def make_date_range(tMin, tMax):
    return DateRange(tMin=tMin, tMax=tMax)

def make_simulation_params(region):
    tMin = FIT_CASE_DATA[region]['tMin'] if region in FIT_CASE_DATA else "2020-03-01"
    tMax = "2020-09-01"
    return SimulationParams(simulationTimeRange=make_date_range(tMin, tMax), **DEFAULTS['SimulationParams'])

def make_containment_params():
    return ContainmentParams(**DEFAULTS['ContainmentParams'])

# TODO: Region and country provide redudant information
#       Condense the information into one field.
def make_params(region, country, population, beds, icus, hemisphere):
    return AllParams(
            population      = make_population_params(region, country, population, beds, icus),
            epidemiological = make_epidemiological_params(region, hemisphere),
            simulation      = make_simulation_params(region),
            containment     = make_containment_params())

# ------------------------------------------------------------------------
# Data fitter

class Fitter:
    doubling_time   = 3.0
    serial_interval = 7.5
    fixed_slope     = np.log(2)/doubling_time
    cases_on_tMin   = 10
    under_reporting = 5
    delay           = 18
    fatality_rate   = 0.02

    def slope_to_r0(self, slope):
        return 1 + slope*self.serial_interval

    def fit(self, pop):
        # ----------------------------------
        # Internal functions

        def fit_cumulative(t, y):
            good_ind    = (y > 3) & (y < 500)
            t_subset    = t[good_ind]
            logy_subset = np.log(y[good_ind])
            num_data    = good_ind.sum()

            if num_data > 10:
                res = linregress(t_subset, logy_subset)
                return {"intercept" : res.intercept,
                        "slope"     : res.slope,
                        'rvalue'    : res.rvalue}
            elif num_data > 4:
                intercept = logy_subset.mean() - t_subset.mean()*self.fixed_slope
                return {"intercept" : intercept,
                        "slope"     : 1.0*self.fixed_slope,
                        'rvalue'    : np.nan}
            else:
                return None

        def to_ms(time):
            return datetime.strptime(time[:10], "%Y-%m-%d").toordinal()

        def from_ms(time):
            d = datetime.fromordinal(int(time))
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"

        # ----------------------------------
        # Body

        data = np.array([ ([to_ms(dp['time']), dp['cases'] or np.nan, dp['deaths'] or np.nan]) for dp in pop ])

        # Try to fit on death
        p = fit_cumulative(data[:,0], data[:,2])
        if p and p["slope"] > 0:
            tMin = (np.log(self.cases_on_tMin * self.fatality_rate) - p["intercept"]) / p["slope"] - self.delay
            return {'tMin': from_ms(tMin), 'initialCases': self.cases_on_tMin, 'r0':self.slope_to_r0(p["slope"])}
        else: # If no death, fit on case counts
            p = fit_cumulative(data[:,0], data[:,1])
            if p and p["slope"] > 0:
                tMin = (np.log(self.cases_on_tMin)/self.under_reporting - p["intercept"]) / p["slope"]
                return {'tMin': from_ms(tMin), 'initialCases': self.cases_on_tMin, 'r0':self.slope_to_r0(p["slope"])}

        return None

# ------------------------------------------------------------------------
# Functions

def marshalJSON(obj, wtr=None):
    if wtr is None:
        return json.dumps(obj, default=lambda x: x.__dict__, sort_keys=True, indent=4)
    else:
        return json.dump(obj, wtr, default=lambda x: x.__dict__, sort_keys=True, indent=4)

def fit_one_case_data(args):
    Params = Fitter()
    region, data = args

    r = fit_population(region)
    if r is None:
        return (region, Params.fit(data))
    else:
        if 'New York' in region:
            print(f"Region {region}: {r['params']}", file=sys.stderr)

    param = {"tMin": r['tMin'], "r0": r['params'].rates.R0, "initialCases": r["initialCases"]}
    return (region, param)

def fit_all_case_data(num_procs=4):
    pool = multi.Pool(num_procs)
    case_counts = parse_tsv()
    results = pool.map(fit_one_case_data, list(case_counts.items()))
    for k, v in results:
        if v is not None:
            FIT_CASE_DATA[k] = v


def set_mitigation(cases, scenario):
    data = np.array([float(x) for x in scenario.containment.reduction])
    timeline = np.linspace(datetime.strptime(scenario.simulation.simulationTimeRange.tMin[:10], '%Y-%m-%d').toordinal(),
                           datetime.strptime(scenario.simulation.simulationTimeRange.tMax[:10], '%Y-%m-%d').toordinal(),
                           len(data))

    valid_cases = [c for c in cases if c['cases'] is not None]
    if len(valid_cases)==0:
        scenario.containment.reduction = [float(x) for x in data]
        return

    case_counts = np.array([c['cases'] for c in valid_cases])
    levelOne = np.where(case_counts > min(max(5, scenario.population.populationServed/1e5),200))[0]
    levelTwo = np.where(case_counts > min(max(5, scenario.population.populationServed/1e3),10000))[0]

    for name, level, val in [("levelOne", levelOne, 0.8), ('levelTwo', levelTwo, 0.6)]:
        if len(level):
            level_idx = level[0]
            cutoff_str = valid_cases[level_idx]["time"][:10]
            cutoff = datetime.strptime(cutoff_str, '%Y-%m-%d').toordinal()

            data[timeline>cutoff] *= val
            # scenario.containment.mitigationIntervals.append(Measure(name, cutoff_str, 
            #     scenario.simulation.simulationTimeRange.tMax[:10], 
            #     val))

    scenario.containment.reduction = [float(x) for x in data]

# ------------------------------------------------------------------------
# Main point of entry

def generate(output_json, num_procs=1):
    scenario = {}
    # fit_all_case_data(num_procs)
    case_counts = parse_tsv()

    with open(SCENARIO_POPS, 'r') as fd:
        rdr = csv.reader(fd, delimiter='\t')
        hdr = next(rdr)
        idx = {'name' : hdr.index('name'),
               'size' : hdr.index('populationServed'),
               'ages' : hdr.index('ageDistribution'),
               'beds' : hdr.index('hospitalBeds'),
               'icus' : hdr.index('ICUBeds'),
               'hemisphere' : hdr.index('hemisphere')}

        args = ['name', 'ages', 'size', 'beds', 'icus', 'hemisphere']
        for region in rdr:
            region_name = region[idx['name']]
            entry = [region[idx[arg]] for arg in args]
            scenario[region_name] = make_params(*entry)
            if region_name in case_counts:
                set_mitigation(case_counts[region_name], scenario[region_name])
            else:
                scenario[region_name].containment.reduction = [float(x)
                    for x in scenario[region_name].containment.reduction]
            break

    with open(output_json, "w+") as fd:
        marshalJSON(scenario, fd)

if __name__ == '__main__':
        generate("tmp.json")