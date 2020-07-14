import os
import sys
import threading
from queue import Queue
from copy import deepcopy

import pandas as pd
from datetime import datetime, timedelta
import time
import numpy as np
import json
import toml
import random
import names
import string
import cvxpy as cp
import dccp
import itertools as it
import redis
from sklearn.linear_model import Ridge
import scipy.stats

# Local
from dragg.mpc_calc import MPCCalc
from dragg.redis_client import RedisClient
from dragg.logger import Logger

def rbf(x, sigma, mu=0):
    return 1/(sigma*np.sqrt(2*np.pi)) * np.exp(-1*(x**2)/(2*sigma**2))

class Aggregator:
    def __init__(self, run_name="outputs"):
        self.agg_log = Logger("aggregator")
        self.mpc_log = Logger("mpc_calc")
        self.forecast_log = Logger("forecaster")
        self.data_dir = 'data'
        self.outputs_dir = os.path.join(run_name)
        if not os.path.isdir(self.outputs_dir):
            os.makedirs(self.outputs_dir)
        self.config_file = os.path.join(self.data_dir, os.environ.get('CONFIG_FILE', 'config.toml'))
        self.ts_data_file = os.path.join(self.data_dir, os.environ.get('SOLAR_TEMPERATURE_DATA_FILE', 'nsrdb.csv'))
        self.spp_data_file = os.path.join(self.data_dir, os.environ.get('SPP_DATA_FILE', 'tou_data.xlsx'))
        # self.required_keys = {
        #     "total_number_homes",
        #     "homes_battery",
        #     "homes_pv",
        #     "homes_pv_battery",
        #     "home_hvac_r_dist",
        #     "home_hvac_c_dist",
        #     "home_hvac_p_cool_dist",
        #     "home_hvac_p_heat_dist",
        #     "wh_r_dist",
        #     "wh_c_dist",
        #     "wh_p_dist",
        #     "battery_max_rate",
        #     "battery_capacity",
        #     "battery_cap_bounds",
        #     "battery_charge_eff",
        #     "battery_discharge_eff",
        #     "pv_area",
        #     "pv_efficiency",
        #     "start_datetime",
        #     "end_datetime",
        #     "mpc_prediction_horizon",
        #     "random_seed",
        #     "load_zone",
        #     # "step_size_coeff", # removed for RL aggregator
        #     # "max_load_threshold", # removed for RL aggregator
        #     "check_type"
        # }
        self.required_keys = None
        self.timestep = None  # Set by redis_set_initial_values
        self.iteration = None  # Set by redis_set_initial_values
        self.reward_price = None  # Set by redis_set_initial_values
        self.start_hour_index = None  # Set by calc_star_hour_index
        # self.horizon = None  # Set by redis_set_initial_values
        # self.agg_load = None  # Set after every iteration
        self.agg_load = 0
        self.baseline_data = {}
        self.baseline_agg_load_list = []  # Aggregate load at every timestep from the baseline run
        self.max_agg_load = None  # Set after baseline run, the maximum aggregate load over all the timesteps
        self.max_agg_load_list = []
        # self.max_agg_load_threshold = None  # Set after baseline run, max_agg_load * threshold value set
        # self.max_agg_load_threshold_list = []
        self.converged = False
        self.num_threads = 1
        self.start_dt = None  # Set by _set_dt
        self.end_dt = None  # Set by _set_dt
        self.hours = None  # Set by _set_dt
        self.dt = None  # Set by _set_dt
        self.num_timesteps = None  # Set by _set_dt
        self.all_homes = None  # Set by create_homes
        self.queue = Queue()
        # self.redis_pool = redis.ConnectionPool(host = os.environ.get('REDIS_HOST', 'localhost'), decode_responses = True, db = 0)
        # self.redis_client = redis.Redis(connection_pool = self.redis_pool)
        self.redis_client = RedisClient()
        self.config = self._import_config()
        # self.step_size_coeff = self.config["step_size_coeff"] # removed for RL aggregator
        self.check_type = self.config['simulation']['check_type']  # One of: 'pv_only', 'base', 'battery_only', 'pv_battery', 'all'

        self.ts_data = self._import_ts_data()  # Temp: degC, RH: %, Pressure: mbar, GHI: W/m2
        self.tou_data = self._import_tou_data()  # SPP: $/kWh
        self.all_data = self.join_data()
        self._set_dt()
        self._build_tou_price()
        self.all_data.drop("ts", axis=1)

        self.all_rps = np.zeros(self.hours * self.dt)
        self.all_sps = np.zeros(self.hours * self.dt)

        self.action = 0
        self.q_tables = []
        self.memory = []
        self.memory_size = 1000
        self.actionspace = self.config['rl']['utility']['action_space']

    def _import_config(self):
        if not os.path.exists(self.config_file):
            self.agg_log.logger.error(f"Configuration file does not exist: {self.config_file}")
            sys.exit(1)
        with open(self.config_file, 'r') as f:
            data = toml.load(f)
            # d_keys = set(data.keys())
            # is_subset = self.required_keys.issubset(d_keys)
            # if not is_subset:
            #     self.agg_log.logger.error(f"Not all required keys specified in config file. These must be specified: {self.required_keys}")
            #     sys.exit(1)
            return data

    def _set_dt(self):
        """
        Convert the start and end datetimes specified in the config file into python datetime
        objects.  Calculate the number of hours for which the simulation will run.
        :return:
        """
        try:
            self.start_dt = datetime.strptime(self.config['simulation']['start_datetime'], '%Y-%m-%d %H')
            self.end_dt = datetime.strptime(self.config['simulation']['end_datetime'], '%Y-%m-%d %H')
        except ValueError as e:
            self.agg_log.logger.error(f"Error parsing datetimes: {e}")
            sys.exit(1)
        self.hours = self.end_dt - self.start_dt
        self.hours = int(self.hours.total_seconds() / 3600)

        self.num_timesteps = self.hours * self.dt
        self.mask = (self.all_data.index >= self.start_dt) & (self.all_data.index < self.end_dt)
        self.agg_log.logger.info(f"Start: {self.start_dt.isoformat()}; End: {self.end_dt.isoformat()}; Number of hours: {self.hours}")

    def _import_ts_data(self):
        """
        Import timeseries data from file downloaded from NREL NSRDB.  The function removes the top two
        lines.  Columns which must be present: ["Year", "Month", "Day", "Hour", "Minute", "Temperature", "GHI"]
        Renames 'Temperature' to 'OAT'
        :return: pandas.DataFrame, columns: ts, GHI, OAT
        """
        if not os.path.exists(self.ts_data_file):
            self.agg_log.logger.error(f"Timeseries data file does not exist: {self.ts_data_file}")
            sys.exit(1)

        df = pd.read_csv(self.ts_data_file, skiprows=2)
        # df = df[df["Minute"] == 0]
        self.dt = int(self.config['rl']['utility']['hourly_steps'])
        self.dt_interval = 60 // self.dt
        reps = [np.ceil(self.dt/2) if val==0 else np.floor(self.dt/2) for val in df.Minute]
        df = df.loc[np.repeat(df.index.values, reps)]
        interval_minutes = self.dt_interval * np.arange(self.dt)
        n_intervals = len(df.index) // self.dt
        x = np.tile(interval_minutes, n_intervals)
        df.Minute = x
        df = df.astype(str)
        df['ts'] = df[["Year", "Month", "Day", "Hour", "Minute"]].apply(lambda x: ' '.join(x), axis=1)
        df = df.rename(columns={"Temperature": "OAT"})
        df["ts"] = df["ts"].apply(lambda x: datetime.strptime(x, '%Y %m %d %H %M'))
        df = df.filter(["ts", "GHI", "OAT"])
        df[["GHI", "OAT"]] = df[["GHI", "OAT"]].astype(int)
        return df.reset_index(drop=True)

    def _import_tou_data(self):
        """
        Settlement Point Price (SPP) data as extracted from ERCOT historical DAM Load Zone and Hub Prices.
        url: http://www.ercot.com/mktinfo/prices.
        Only keeps SPP data, converts to $/kWh.
        Subtracts 1 hour from time to be inline with 23 hour day as required by pandas.
        :return: pandas.DataFrame, columns: ts, SPP
        """
        if not os.path.exists(self.spp_data_file):
            self.agg_log.logger.error(f"TOU data file does not exist: {self.spp_data_file}")
            sys.exit(1)
        df_all = pd.read_excel(self.spp_data_file, sheet_name=None)
        k1 = list(df_all.keys())[0]
        df = df_all[k1]
        for k, v in df_all.items():
            if k == k1:
                pass
            else:
                df = df.append(v, ignore_index=True)

        df = df[df["Settlement Point"] == self.config['simulation']['load_zone']]
        df["Hour Ending"] = df["Hour Ending"].str.replace(':00', '')
        df["Hour Ending"] = df["Hour Ending"].apply(pd.to_numeric)
        df["Hour Ending"] = df["Hour Ending"].apply(lambda x: x - 1)
        df["Hour Ending"] = df["Hour Ending"].astype(str)
        df['ts'] = df[["Delivery Date", "Hour Ending"]].apply(lambda x: ' '.join(x), axis=1)
        df = df.drop(columns=['Delivery Date', 'Hour Ending', 'Repeated Hour Flag', 'Settlement Point'])
        df = df.rename(columns={"Settlement Point Price": "SPP"})
        col_order = ["ts", "SPP"]
        df = df[col_order]
        df[["ts"]] = df.loc[:, "ts"].apply(lambda x: datetime.strptime(x, '%m/%d/%Y %H'))
        df[["SPP"]] = df.loc[:, "SPP"].apply(lambda x: x / 1000)
        return df.reset_index(drop=True)

    def _build_tou_price(self):
        try:
            sd_times = self.config["shoulder_times"]
            pk_times = self.config["peak_times"]
            op_price = float(self.config["offpeak_price"])
            sd_price = float(self.config["shoulder_price"])
            pk_price = float(self.config["peak_price"])
            self.all_data['tou'] = self.all_data['ts'].apply(lambda x: pk_price if (x.hour <= pk_times[1] and x.hour >= pk_times[0]) else (sd_price if x.hour <= sd_times[1] and x.hour >= sd_times[0] else op_price))
        except:
            self.all_data['tou'] = float(self.config['rl']['utility']['base_price'])

    def join_data(self):
        """
        Join the TOU, GHI, temp data into a single dataframe
        :return: pandas.DataFrame
        """
        df = pd.merge(self.ts_data, self.tou_data, how='outer', on='ts')
        df = df.fillna(method='ffill')
        return df.set_index('ts', drop=False)

    def _check_home_configs(self):
        base_homes = [e for e in self.all_homes if e["type"] == "base"]
        pv_battery_homes = [e for e in self.all_homes if e["type"] == "pv_battery"]
        pv_only_homes = [e for e in self.all_homes if e["type"] == "pv_only"]
        battery_only_homes = [e for e in self.all_homes if e["type"] == "battery_only"]
        if not len(base_homes) == self.config['community']['total_number_homes'] - self.config['community']['homes_battery'] - self.config['community']['homes_pv'] - self.config['community']['homes_pv_battery']:
            self.agg_log.logger.error("Incorrect number of base homes.")
            sys.exit(1)
        elif not len(pv_battery_homes) == self.config['community']['homes_pv_battery']:
            self.agg_log.logger.error("Incorrect number of base pv_battery homes.")
            sys.exit(1)
        elif not len(pv_only_homes) == self.config['community']['homes_pv']:
            self.agg_log.logger.error("Incorrect number of base pv_only homes.")
            sys.exit(1)
        elif not len(battery_only_homes) == self.config['community']['homes_battery']:
            self.agg_log.logger.error("Incorrect number of base pv_only homes.")
            sys.exit(1)
        else:
            self.agg_log.logger.info("Homes looking ok!")

    def reset_seed(self, new_seed):
        """
        Reset value for seed.
        :param new_seed: int
        :return:
        """
        self.config['simulation']['random_seed'] = new_seed

    def create_homes(self):
        """
        Given parameter distributions and number of homes of each type, create a list
        of dictionaries of homes with the parameters set for each home.
        :return:
        """
        # Set seed before sampling.  Will ensure home name and parameters
        # are the same throughout different runs
        np.random.seed(self.config['simulation']['random_seed'])
        random.seed(self.config['simulation']['random_seed'])

        # Define home and HVAC parameters
        home_r_dist = np.random.uniform(
            self.config['home']['hvac']['r_dist'][0],
            self.config['home']['hvac']['r_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_c_dist = np.random.uniform(
            self.config['home']['hvac']['c_dist'][0],
            self.config['home']['hvac']['c_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_hvac_p_cool_dist = np.random.uniform(
            self.config['home']['hvac']['p_cool_dist'][0],
            self.config['home']['hvac']['p_cool_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_hvac_p_heat_dist = np.random.uniform(
            self.config['home']['hvac']['p_heat_dist'][0],
            self.config['home']['hvac']['p_heat_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_hvac_temp_in_sp_dist = np.random.uniform(
            self.config['home']['hvac']['temp_sp_dist'][0],
            self.config['home']['hvac']['temp_sp_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_hvac_temp_in_db_dist = np.random.uniform(
            self.config['home']['hvac']['temp_deadband_dist'][0],
            self.config['home']['hvac']['temp_deadband_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_hvac_temp_in_min_dist = home_hvac_temp_in_sp_dist - 0.5 * home_hvac_temp_in_db_dist
        home_hvac_temp_in_max_dist = home_hvac_temp_in_sp_dist + 0.5 * home_hvac_temp_in_db_dist
        home_hvac_temp_init = []
        for i in range(len(home_hvac_temp_in_min_dist)):
            home_hvac_temp_init.append(home_hvac_temp_in_min_dist[i] + np.random.uniform(0, home_hvac_temp_in_db_dist[i]))

        # Define water heater parameters
        wh_r_dist = np.random.uniform(
            self.config['home']['wh']['r_dist'][0],
            self.config['home']['wh']['r_dist'][1],
            self.config['community']['total_number_homes']
        )
        wh_c_dist = np.random.uniform(
            self.config['home']['wh']['c_dist'][0],
            self.config['home']['wh']['c_dist'][1],
            self.config['community']['total_number_homes']
        )
        wh_p_dist = np.random.uniform(
            self.config['home']['wh']['p_dist'][0],
            self.config['home']['wh']['p_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_wh_temp_sp_dist = np.random.uniform(
            self.config['home']['wh']['sp_dist'][0],
            self.config['home']['wh']['sp_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_wh_temp_db_dist = np.random.uniform(
            self.config['home']['wh']['deadband_dist'][0],
            self.config['home']['wh']['deadband_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_wh_temp_min_dist = home_wh_temp_sp_dist - 0.5 * home_wh_temp_db_dist
        home_wh_temp_max_dist = home_wh_temp_sp_dist + 0.5 * home_wh_temp_db_dist
        home_wh_temp_init = []
        for i in range(len(home_wh_temp_max_dist)):
            home_wh_temp_init.append(home_wh_temp_min_dist[i] + np.random.uniform(0, home_wh_temp_db_dist[i]))

        # define water heater draw events
        home_wh_size_dist = np.random.uniform(
            self.config['home']['wh']['size_dist'][0],
            self.config['home']['wh']['size_dist'][1],
            self.config['community']['total_number_homes']
        )
        home_wh_size_dist = (home_wh_size_dist + 10) // 20 * 20 # more even numbers

        ndays = self.num_timesteps // (24 * self.dt) + 1
        daily_timesteps = 24 * self.dt

        home_wh_all_draw_timing_dist = []
        home_wh_all_draw_size_dist = []
        for i in range(self.config['community']['total_number_homes']):
            n_daily_draws = np.random.randint(self.config['home']['wh']['waterdraws']['n_big_draw_dist'][0], self.config['home']['wh']['waterdraws']['n_big_draw_dist'][1]+1)
            typ_draw_times = np.random.randint(0, 24*self.dt, n_daily_draws)
            perturbations = np.array([])
            for d in range(ndays):
                perturbations = np.concatenate((perturbations, (np.random.randint(-1 * self.dt, self.dt, n_daily_draws) + (d * daily_timesteps))))
            big_draw_times = (np.tile(typ_draw_times, ndays) + perturbations)
            big_draw_sizes = (np.random.uniform(self.config['home']['wh']['waterdraws']['big_draw_size_dist'][0], self.config['home']['wh']['waterdraws']['big_draw_size_dist'][1], ndays * n_daily_draws))

            n_daily_draws = np.random.randint(self.config['home']['wh']['waterdraws']['n_small_draw_dist'][0], self.config['home']['wh']['waterdraws']['n_small_draw_dist'][1]+1)
            typ_draw_times = np.random.randint(0, 24*self.dt, n_daily_draws)
            perturbations = np.array([])
            for d in range(ndays):
                perturbations = np.concatenate((perturbations, (np.random.randint(-3 * self.dt, 3 * self.dt, n_daily_draws) + (d * daily_timesteps))))
            small_draw_times = (np.tile(typ_draw_times, ndays) + perturbations)
            small_draw_sizes = (np.random.uniform(self.config['home']['wh']['waterdraws']['small_draw_size_dist'][0], self.config['home']['wh']['waterdraws']['small_draw_size_dist'][1], ndays * n_daily_draws))

            all_draw_times = np.concatenate((big_draw_times, small_draw_times))
            all_draw_sizes = np.concatenate((big_draw_sizes, small_draw_sizes))
            ind = np.argsort(all_draw_times)
            all_draw_times = all_draw_times[ind].tolist()
            all_draw_sizes = all_draw_sizes[ind].tolist()

            home_wh_all_draw_timing_dist.append(all_draw_times)
            home_wh_all_draw_size_dist.append(all_draw_sizes)

        all_homes = []

        # PV values are constant
        pv = {
            "area": self.config['home']['pv']['area'],
            "eff": self.config['home']['pv']['efficiency']
        }

        # battery values also constant
        battery = {
            "max_rate": self.config['home']['battery']['max_rate'],
            "capacity": self.config['home']['battery']['capacity'],
            "capacity_lower": self.config['home']['battery']['cap_bounds'][0] * self.config['home']['battery']['capacity'],
            "capacity_upper": self.config['home']['battery']['cap_bounds'][1] * self.config['home']['battery']['capacity'],
            "ch_eff": self.config['home']['battery']['charge_eff'],
            "disch_eff": self.config['home']['battery']['discharge_eff'],
            "batt_cons": self.config['home']['battery']['cons_penalty'],
            "e_batt_init": np.random.uniform(self.config['home']['battery']['cap_bounds'][0] * self.config['home']['battery']['capacity'],
                                            self.config['home']['battery']['cap_bounds'][1] * self.config['home']['battery']['capacity'])
        }

        i = 0
        # Define pv and battery homes
        for _ in range(self.config['community']['homes_pv_battery']):
            res = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
            all_homes.append({
                "name": names.get_first_name() + '-' + res,
                "type": "pv_battery",
                "hvac": {
                    "r": home_r_dist[i],
                    "c": home_c_dist[i],
                    "p_c": home_hvac_p_cool_dist[i],
                    "p_h": home_hvac_p_heat_dist[i],
                    "temp_in_min": home_hvac_temp_in_min_dist[i],
                    "temp_in_max": home_hvac_temp_in_max_dist[i],
                    "temp_in_sp": home_hvac_temp_in_sp_dist[i],
                    "temp_in_init": home_hvac_temp_init[i]
                },
                "wh": {
                    "r": wh_r_dist[i],
                    "c": wh_c_dist[i],
                    "p": wh_p_dist[i],
                    "temp_wh_min": home_wh_temp_min_dist[i],
                    "temp_wh_max": home_wh_temp_max_dist[i],
                    "temp_wh_sp": home_wh_temp_sp_dist[i],
                    "temp_wh_init": home_wh_temp_init[i],
                    "tank_size": home_wh_size_dist[i],
                    "draw_times": home_wh_all_draw_timing_dist[i],
                    "draw_sizes": home_wh_all_draw_size_dist[i]
                },
                "battery": battery,
                "pv": pv
            })
            i += 1

        # Define pv only homes
        for _ in range(self.config['community']['homes_pv']):
            res = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
            all_homes.append({
                "name": names.get_first_name() + '-' + res,
                "type": "pv_only",
                "hvac": {
                    "r": home_r_dist[i],
                    "c": home_c_dist[i],
                    "p_c": home_hvac_p_cool_dist[i],
                    "p_h": home_hvac_p_heat_dist[i],
                    "temp_in_min": home_hvac_temp_in_min_dist[i],
                    "temp_in_max": home_hvac_temp_in_max_dist[i],
                    "temp_in_sp": home_hvac_temp_in_sp_dist[i],
                    "temp_in_init": home_hvac_temp_init[i]
                },
                "wh": {
                    "r": wh_r_dist[i],
                    "c": wh_c_dist[i],
                    "p": wh_p_dist[i],
                    "temp_wh_min": home_wh_temp_min_dist[i],
                    "temp_wh_max": home_wh_temp_max_dist[i],
                    "temp_wh_sp": home_wh_temp_sp_dist[i],
                    "temp_wh_init": home_wh_temp_init[i],
                    "tank_size": home_wh_size_dist[i],
                    "draw_times": home_wh_all_draw_timing_dist[i],
                    "draw_sizes": home_wh_all_draw_size_dist[i]
                },
                "pv": pv
            })
            i += 1

        # Define battery only homes
        for _ in range(self.config['community']['homes_battery']):
            res = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
            all_homes.append({
                "name": names.get_first_name() + '-' + res,
                "type": "battery_only",
                "hvac": {
                    "r": home_r_dist[i],
                    "c": home_c_dist[i],
                    "p_c": home_hvac_p_cool_dist[i],
                    "p_h": home_hvac_p_heat_dist[i],
                    "temp_in_min": home_hvac_temp_in_min_dist[i],
                    "temp_in_max": home_hvac_temp_in_max_dist[i],
                    "temp_in_sp": home_hvac_temp_in_sp_dist[i],
                    "temp_in_init": home_hvac_temp_init[i]
                },
                "wh": {
                    "r": wh_r_dist[i],
                    "c": wh_c_dist[i],
                    "p": wh_p_dist[i],
                    "temp_wh_min": home_wh_temp_min_dist[i],
                    "temp_wh_max": home_wh_temp_max_dist[i],
                    "temp_wh_sp": home_wh_temp_sp_dist[i],
                    "temp_wh_init": home_wh_temp_init[i],
                    "tank_size": home_wh_size_dist[i],
                    "draw_times": home_wh_all_draw_timing_dist[i],
                    "draw_sizes": home_wh_all_draw_size_dist[i]
                },
                "battery": battery
            })
            i += 1

        base_homes = self.config['community']['total_number_homes'] - self.config['community']['homes_battery'] - self.config['community']['homes_pv'] - self.config['community']['homes_pv_battery']
        for _ in range(base_homes):
            res = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
            all_homes.append({
                "name": names.get_first_name() + '-' + res,
                "type": "base",
                "hvac": {
                    "r": home_r_dist[i],
                    "c": home_c_dist[i],
                    "p_c": home_hvac_p_cool_dist[i],
                    "p_h": home_hvac_p_heat_dist[i],
                    "temp_in_min": home_hvac_temp_in_min_dist[i],
                    "temp_in_max": home_hvac_temp_in_max_dist[i],
                    "temp_in_sp": home_hvac_temp_in_sp_dist[i],
                    "temp_in_init": home_hvac_temp_init[i]
                },
                "wh": {
                    "r": wh_r_dist[i],
                    "c": wh_c_dist[i],
                    "p": wh_p_dist[i],
                    "temp_wh_min": home_wh_temp_min_dist[i],
                    "temp_wh_max": home_wh_temp_max_dist[i],
                    "temp_wh_sp": home_wh_temp_sp_dist[i],
                    "temp_wh_init": home_wh_temp_init[i],
                    "tank_size": home_wh_size_dist[i],
                    "draw_times": home_wh_all_draw_timing_dist[i],
                    "draw_sizes": home_wh_all_draw_size_dist[i]
                }
            })
            i += 1

        self.all_homes = all_homes
        self._check_home_configs()

    def reset_baseline_data(self):
        self.baseline_agg_load_list = []
        for home in self.all_homes:
            self.baseline_data[home["name"]] = {
                "type": home["type"],
                "temp_in_sp": home["hvac"]["temp_in_sp"],
                "temp_wh_sp": home["wh"]["temp_wh_sp"],
                "temp_in_opt": [home["hvac"]["temp_in_init"]],
                "temp_wh_opt": [home["wh"]["temp_wh_init"]],
                "p_grid_opt": [],
                "p_load_opt": [],
                "hvac_cool_on_opt": [],
                "hvac_heat_on_opt": [],
                "wh_heat_on_opt": [],
                "cost_opt": [],
            }
            if 'pv' in home["type"]:
                self.baseline_data[home["name"]]["p_pv_opt"] = []
                self.baseline_data[home["name"]]["u_pv_curt_opt"] = []
            if 'battery' in home["type"]:
                self.baseline_data[home["name"]]["e_batt_opt"] = [home["battery"]["e_batt_init"]]
                self.baseline_data[home["name"]]["p_batt_ch"] = []
                self.baseline_data[home["name"]]["p_batt_disch"] = []

    def check_all_data_indices(self):
        """
        Ensure enough data exists in all_data such that MPC calcs can be made throughout
        the requested start and end period.
        :return:
        """
        if not self.start_dt >= self.all_data.index[0]:
            self.agg_log.logger.error("The start datetime must exist in the data provided.")
            sys.exit(1)
        if not self.end_dt + timedelta(hours=max(self.config['home']['hems']['prediction_horizon'])) <= self.all_data.index[-1]:
            self.agg_log.logger.error("The end datetime + the largest prediction horizon must exist in the data provided.")
            sys.exit(1)

    def calc_start_hour_index(self):
        """
        Since all_data is posted as a list, where 0 corresponds to the first hour in
        the dataframe, the number of hours between the start_dt and the above mentioned
        hour needs to be calculated.
        :return:
        """
        start_hour_index = self.start_dt - self.all_data.index[0]
        self.start_hour_index = int(start_hour_index.total_seconds() / 3600)

    def redis_set_initial_values(self):
        """
        Set the initial timestep, iteration, reward price, and horizon to redis
        :return:
        """
        self.timestep = 0
        # self.reward_price = np.zeros(self.rl_agg_horizon)

        # min_runtime = self.config["min_runtime_mins"]
        self.e_batt_init = self.config['home']['battery']['capacity'] * self.config['home']['battery']['cap_bounds'][0]
        self.redis_client.conn.hset("initial_values", "e_batt_init", self.e_batt_init)
        self.redis_client.conn.set("start_hour_index", self.start_hour_index)
        self.redis_client.conn.hset("current_values", "timestep", self.timestep)

        if self.case == "agg_mpc":
            self.iteration = 0
            self.redis_client.conn.hset("current_values", "iteration", self.iteration)
            self.redis_client.conn.hset("current_values", "reward_price", self.reward_price.tolist())

        if self.case == "rl_agg":
            self.reward_price = np.zeros(self.rl_agg_horizon * self.dt)
            for val in self.reward_price.tolist():
                self.redis_client.conn.rpush("reward_price", val)

        if self.case == "simplified":
            self.reward_price = np.zeros(1) # force immediate response to action price in simplified case
            for val in self.reward_price.tolist():
                self.redis_client.conn.rpush("reward_price", val)

    def redis_set_state_for_previous_timestep(self):
        """
        This is used for the AGG MPC implementation during back and forth iterations with the
        individual home in order to ensure, regardless of the iteration, the home always solves
        the problem using the previous states.  The previous optimal vals set by each home after
        converging need to be reset to reflect previous optimal state.
        :return:
        """
        for home, vals in self.baseline_data.items():
            for k, v in vals.items():
                if k == "temp_in_opt":
                    self.redis_client.conn.hset(home, k, v[-1])
                elif k == "temp_wh_opt":
                    self.redis_client.conn.hset(home, k, v[-1])
                if 'battery' in vals['type'] and k == "e_batt_opt":
                    self.redis_client.conn.hset(home, k, v[-1])

    def redis_add_all_data(self):
        """
        Values for the timeseries data are written to Redis as a list, where the
        column names: [GHI, OAT, SPP] are the redis keys.  Each list is as long
        as the data in self.all_data, which is 8760.
        :return:
        """
        for c in self.all_data.columns.to_list():
            data = self.all_data[c]
            for val in data.values.tolist():
                self.redis_client.conn.rpush(c, val)

    def redis_set_current_values(self):
        self.redis_client.conn.hset("current_values", "timestep", self.timestep)

        if self.case == "agg_mpc":
            self.redis_client.conn.hset("current_values", "iteration", self.iteration)
        elif self.case == "rl_agg" or self.case == "simplified":
            self.all_sps[self.timestep] = self.agg_setpoint
            self.all_rps[self.timestep] = self.reward_price[0]
            for val in self.reward_price.tolist():
                self.redis_client.conn.lpop("reward_price")
                self.redis_client.conn.rpush("reward_price", val)

    def _calc_state(self):
        current_error = (self.agg_load - self.agg_setpoint) #/ self.agg_setpoint
        if self.timestep > 0:
            integral_error = self.state["int_error"] + current_error # calls previous state
            derivative_error = self.state["curr_error"] - current_error
        else:
            integral_error = 0
            derivative_error = 0
        derivative_action = self.action - self.prev_action
        change_rp = self.reward_price[0] - self.reward_price[-1]
        time_of_day = self.timestep % (24 * self.dt)
        forecast_error = self.forecast_load[0] - self.forecast_setpoint
        forecast_trend = self.forecast_load[0] - self.forecast_load[-1]

        return {"curr_error":current_error,
        "time_of_day":time_of_day,
        "int_error":integral_error,
        "fcst_error":forecast_error,
        "forecast_trend": forecast_trend,
        "delta_action": change_rp}

    def _reward(self, x):
        """
        @kyri: Reward "function" should encourage the RL agent to move towards a state with curr_error = 0
        :return: float
        """

        reward = -1*self.state["curr_error"]**2 #+ self.reward_price[-1]**2

        return reward

    def _experience(self):
        experience = {"state": self.state, "action": self.action, "next_state": self.next_state, "reward": self.reward}
        self.memory.append(experience)
        return experience

    def _state_basis(self, state):
        forecast_error_basis = np.array([1, state["fcst_error"], state["fcst_error"]**2])
        forecast_trend_basis = np.array([1, state["forecast_trend"], state["forecast_trend"]**2])
        time_basis = np.array([1, np.sin(2 * np.pi * state["time_of_day"]), np.cos(2 * np.pi * state["time_of_day"])])

        state_basis = np.outer(forecast_error_basis, forecast_trend_basis).flatten()[1:]
        state_basis = np.outer(state_basis, time_basis).flatten()[1:]

        return state_basis

    def _phi(self, state, action):
        """
        @kyri: Phi = the basis functions for the Q-function, the values and length of phi are dynamic so any changes here should be fine.
        :return: a 1-D numpy array of arbitrary length
        """

        # action_basis = np.array([1, (action), (action)**2, ((action - 0.02))**2, ((action + 0.02))**2])
        action_basis = np.array([1, action, action**2])
        delta_action_basis = np.array([1, state["delta_action"], state["delta_action"]**2])
        time_basis = np.array([1, np.sin(2 * np.pi * state["time_of_day"]), np.cos(2 * np.pi * state["time_of_day"])])
        curr_error_basis = np.array([1, state["curr_error"], state["curr_error"]**2])
        forecast_error_basis = np.array([1, state["fcst_error"], state["fcst_error"]**2])
        forecast_trend_basis = np.array([1, state["forecast_trend"], state["forecast_trend"]**2])

        # v = np.outer(avg_forecast_error_basis, action_basis).flatten()[1:] #14 (indexed to 13)
        w = np.outer(curr_error_basis, delta_action_basis).flatten()[1:]
        v = np.outer(forecast_trend_basis, action_basis).flatten()[1:]
        w = np.outer(forecast_error_basis, action_basis).flatten()[1:] #8
        z = np.outer(action_basis, curr_error_basis).flatten()[1:] #14
        phi = np.concatenate((v, w, z))
        phi = np.outer(phi, time_basis).flatten()[1:]

        # phi = np.clip(phi, -100, 150)
        return phi

        # # # return np.array([1, state["percent_error"], state["percent_error"]**2, np.sin(2 * np.pi * state["time_of_day"]), np.cos(2 * np.pi * state["time_of_day"])])
        # # # error_normalized = np.clip(state["percent_error"] + 0.5, 0, 1)
        # c = [0, 0.5, 1, 2, 4]
        # # scale state values to roughly 1
        # vals = [state["curr_error"] / self.agg_setpoint, state["forecast_trend"], state["time_of_day"], action*10+.5]
        #
        # x = []
        # k = 2 # fourier basis of dimension 2
        # s_pairs = it.combinations(vals, k) #
        # c_pairs = it.combinations(vals, k) # coefficient pairs
        # for s in s_pairs:
        #     for c in c_pairs:
        #         arg = np.pi * (np.array(s) @ np.array(c))
        #         x.append(np.cos(arg))
        # x = np.array(x)
        # return x

    def _q(self, state, action):
        return self.theta @ self._phi(state, action)

    def _get_greedyaction(self, state):
        self.q_lookup = np.arange(self.actionspace[0], self.actionspace[1]+0.009, 0.01) # to make actionspace inclusive
        self.nActions = len(self.q_lookup)
        self.q_lookup = np.column_stack((self.q_lookup, self.q_lookup))
        for i in range(len(self.q_lookup)):
            self.q_lookup[i,1] = self._q(state, self.q_lookup[i,0])

        self.q_tables.append(self.q_lookup.tolist())

        index = np.argmax(self.q_lookup[:,1])
        u_k_opt = self.q_lookup[index,0]
        q_max = self.q_lookup[index,1]

        return u_k_opt

    def _get_policyaction(self, state):
        pi = []
        x_k = self._state_basis(self.state)
        mu = self.theta_mu @ x_k
        mu = np.clip(mu, self.actionspace[0], self.actionspace[1])
        # arg = self.theta_sigma @ x_k
        # sigma = np.exp(arg)
        sigma = self.config['rl']['parameters']['variance']

        action = scipy.stats.norm.rvs(loc=mu, scale=sigma)
        action = np.clip(action, self.actionspace[0], self.actionspace[1])

        return action

    def _value(self, state):
        return self.w @ self._state_basis(state)

    def update_policy(self):
        x_k = self._state_basis(self.state)
        x_k1 = self._state_basis(self.next_state)
        delta = self.q_predicted - self.q_observed
        delta = np.clip(delta, -1, 1)
        self.average_reward += self.alpha_r * delta
        self.z_w = self.lam_w * self.z_w + (x_k1 - x_k)
        mu = self.theta_mu @ x_k
        # sigma = np.exp(self.theta_sigma @ x_k)
        sigma = self.config['rl']['parameters']['variance']
        grad_pi_mu = (sigma**2) * (self.action - mu) * x_k
        # grad_pi_sigma = ((self.action - mu)**2 / sigma**2 - 1) * x_k
        self.z_theta_mu = self.lam_theta * self.z_theta_mu + (grad_pi_mu)
        # self.z_theta_sigma = self.lam_theta * self.z_theta_sigma + (grad_pi_sigma)
        self.w += self.alpha_w * delta * self.z_w # update reward function
        self.theta_mu += self.alpha_theta * delta * self.z_theta_mu
        # self.theta_sigma += self.alpha_theta * delta * self.z_theta_sigma

    def update_qfunction(self, theta):
        self.q_predicted = theta @ self._phi(self.state, self.action) # recorded for analysis
        self.q_observed = self.reward - self.average_reward + self.beta * theta @ self._phi(self.next_state, self.next_action) # recorded for analysis
        temp_theta = deepcopy(theta)

        if len(self.memory) > self.batch_size:
            batch = random.sample(self.memory, self.batch_size)
            batch_y = []
            batch_phi = []
            for exp in batch:
                x = exp["state"]
                x1 = exp["next_state"]
                u = exp["action"]
                # u1 = self._get_greedyaction(x1)
                u1 = self._get_policyaction(x1)
                xu_k = self._phi(x,u)
                xu_k1 = self._phi(x1,u1)
                q1_a = self.theta_a @ xu_k1
                q1_b = self.theta_b @ xu_k1
                y = exp["reward"] + self.beta * min(q1_a, q1_b)
                batch_y.append(y)
                batch_phi.append(xu_k)
            batch_y = np.array(batch_y)
            batch_phi = np.array(batch_phi)

            if np.isnan(batch_y).any():
                print("problem in y")
                if np.isnan(self.theta_a).any() or np.isnan(self.theta_b).any():
                    print("problem in theta")
            if np.isnan(batch_phi).any():
                print("problem in phi")

            clf = Ridge(alpha = 0.01)
            clf.fit(batch_phi, batch_y)
            temp_theta = clf.coef_
            theta = self.alpha * temp_theta + (1-self.alpha) * theta

            if (self.timestep + 1) % 10 == 0:
                self.update_policy()
        return theta

    def rl_update_reward_price(self):
        self.reward_price[:-1] = self.reward_price[1:]
        self.reward_price[-1] = self.action/100

    def _get_action(self, state): # action is the change in RP from the average RP in the last h timesteps
        if self.rl_agg_horizon > 1:
            avg_rp = np.sum(self.reward_price[1:]) / (self.rl_agg_horizon - 1)
        else:
            avg_rp = self.reward_price[0]
        # update epsilon
        # if ((self.timestep+(48*self.dt+1)) % (48*self.dt)) == 0: # every other day
        #     self.epsilon = self.epsilon/2 # decrease exploration rate
        if np.random.uniform(0,1) >= self.epsilon: # the greedy action
            u_k = self.next_greedy_action
            self.is_greedy = True
            self.agg_log.logger.info("Selecting greedy action.")
        else: # exploration
            u_k = random.uniform(self.actionspace[0], self.actionspace[1])
            self.agg_log.logger.info("Selecting non-greedy action.")
            self.is_greedy = False

        action = u_k
        return action

    def check_baseline_vals(self):
        for home, vals in self.baseline_data.items():
            if self.check_type == 'all':
                homes_to_check = self.all_homes
            else:
                homes_to_check = [x for x in self.all_homes if x["type"] == self.check_type]
            if home in homes_to_check:
                for k, v2 in vals.items():
                    if k in ["temp_in_opt", "temp_wh_opt", "e_batt_opt"] and len(v2) != self.hours + 1:
                        self.agg_log.logger.error(f"Incorrect number of hours. {home}: {k} {len(v2)}")
                    elif len(v2) != self.hours:
                        self.agg_log.logger.error(f"Incorrect number of hours. {home}: {k} {len(v2)}")

    def run_iteration(self, horizon=1):
        worker = MPCCalc(self.queue, horizon, self.dt, self.mpc_discomfort, self.mpc_disutility, self.case, self.redis_client, self.mpc_log)
        worker.run()

        # Block in Queue until all tasks are done
        self.queue.join()

        self.agg_log.logger.info(f"Workers complete for timestep {self.timestep} of {self.num_timesteps}.")
        self.agg_log.logger.info(f"Number of threads: {threading.active_count()}.")
        self.agg_log.logger.info(f"Length of queue: {self.queue.qsize()}.")

    def collect_data(self):
        agg_load = 0
        agg_cost = 0
        for home in self.all_homes:
            if self.check_type == 'all' or home["type"] == self.check_type:
                vals = self.redis_client.conn.hgetall(home["name"])
                for k, v in vals.items():
                    self.baseline_data[home["name"]][k].append(float(v))
                agg_load += float(vals["p_grid_opt"])
                agg_cost += float(vals["cost_opt"])
        self.agg_load = agg_load
        self.agg_cost = agg_cost
        self.baseline_agg_load_list.append(self.agg_load)

    def record_rl_q_data(self):
        self.rl_q_data["timestep"].append(self.timestep)
        self.rl_q_data["theta"].append(self.theta.flatten().tolist())
        self.rl_q_data["q_obs"].append(self.q_observed)
        self.rl_q_data["q_pred"].append(self.q_predicted)
        self.rl_q_data["action"].append(self.action)
        self.rl_q_data["is_greedy"].append(self.is_greedy)
        self.rl_q_data["average_reward"].append(self.average_reward)
        self.rl_q_data["cumulative_reward"].append(self.cumulative_reward)
        self.rl_q_data["reward"].append(self.reward)

    def run_baseline(self, horizon=1):
        self.agg_log.logger.info(f"Performing baseline run for horizon: {horizon}")
        self.start_time = datetime.now()
        for t in range(self.num_timesteps):
            for home in self.all_homes:
                if self.check_type == "all" or home["type"] == self.check_type:
                    self.queue.put(home)
            self.redis_set_current_values()
            self.run_iteration(horizon)
            self.collect_data()
            self.timestep += 1
        # Write
        self.end_time = datetime.now()
        self.t_diff = self.end_time - self.start_time
        self.agg_log.logger.info(f"Horizon: {horizon}; Num Hours Simulated: {self.hours}; Run time: {self.t_diff.total_seconds()} seconds")
        self.check_baseline_vals()

    def summarize_baseline(self, horizon=1):
        """
        Get the maximum of the aggregate demand
        :return:
        """
        self.max_agg_load = max(self.baseline_agg_load_list)
        # self.max_agg_load_threshold = self.max_agg_load * self.max_load_threshold
        self.max_agg_load_list.append(self.max_agg_load)
        # self.max_agg_load_threshold_list.append(self.max_agg_load_threshold)

        self.agg_log.logger.info(f"Max load list: {self.max_agg_load_list}")
        self.baseline_data["Summary"] = {
            "case": self.case,
            "start_datetime": self.start_dt.strftime('%Y-%m-%d %H'),
            "end_datetime": self.end_dt.strftime('%Y-%m-%d %H'),
            "solve_time": self.t_diff.total_seconds(),
            "horizon": horizon,
            "num_homes": self.config['community']['total_number_homes'],
            "p_max_aggregate": self.max_agg_load,
            # "p_max_aggregate_threshold": self.max_agg_load_threshold,
            "p_grid_aggregate": self.baseline_agg_load_list,
            "SPP": self.all_data.loc[self.mask, "SPP"].values.tolist(),
            "OAT": self.all_data.loc[self.mask, "OAT"].values.tolist(),
            "GHI": self.all_data.loc[self.mask, "GHI"].values.tolist(),
            "TOU": self.all_data.loc[self.mask, "tou"].values.tolist(),
            "RP": self.all_rps.tolist(),
            "p_grid_setpoint": self.all_sps.tolist()
        }

    def write_outputs(self, horizon):
        # Write values for baseline run to file

        date_output = os.path.join(self.outputs_dir, f"{self.start_dt.strftime('%Y-%m-%dT%H')}_{self.end_dt.strftime('%Y-%m-%dT%H')}")
        if not os.path.isdir(date_output):
            os.makedirs(date_output)

        mpc_output = os.path.join(date_output, f"{self.check_type}-homes_{self.config['community']['total_number_homes']}-horizon_{horizon}-interval_{self.dt_interval}")
        if not os.path.isdir(mpc_output):
            os.makedirs(mpc_output)

        agg_output = os.path.join(mpc_output, f"{self.case}")
        if not os.path.isdir(agg_output):
            os.makedirs(agg_output)

        if self.case == "baseline" or self.case == "no_mpc":
            file_name = f"{self.case}_discomf-{self.mpc_discomfort}-results.json"

        elif self.case == "agg_mpc":
            file_name = "results.json"
            f2 = os.path.join(agg_output, f"{self.check_type}-homes_{self.config['community']['total_number_homes']}-horizon_{horizon}-iter-results.json")
            with open(f2, 'w+') as f:
                json.dump(self.agg_mpc_data, f, indent=4)

        else: # self.case == "rl_agg" or self.case == "simplified"
            file_name = f"agg_horizon_{self.rl_agg_horizon}-alpha_{self.alpha}-epsilon_{self.epsilon_init}-beta_{self.beta}_batch-{self.batch_size}_disutil-{self.mpc_disutility}_discomf-{self.mpc_discomfort}-results.json"
            f4 = os.path.join(agg_output, f"agg_horizon_{self.rl_agg_horizon}-alpha_{self.alpha}-epsilon_{self.epsilon_init}-beta_{self.beta}_batch-{self.batch_size}_disutil-{self.mpc_disutility}_discomf-{self.mpc_discomfort}-q-results.json")
            with open(f4, 'w+') as f:
                json.dump(self.rl_q_data, f, indent=4)

        file = os.path.join(agg_output, file_name)
        with open(file, 'w+') as f:
            json.dump(self.baseline_data, f, indent=4)

    def write_home_configs(self):
        # Write all home configurations to file
        ah = os.path.join(self.outputs_dir, f"all_homes-{self.config['community']['total_number_homes']}-config.json")
        with open(ah, 'w+') as f:
            json.dump(self.all_homes, f, indent=4)

    def set_agg_mpc_initial_vals(self):
        temp = []
        for h in range(self.hours):
            temp.append({
                "timestep": h,
                "reward_price": [],
                "agg_cost": [],
                "agg_load": []
            })
        return temp

    def run_agg_mpc(self, horizon):
        self.agg_log.logger.info(f"Performing AGG MPC run for horizon: {horizon}")
        self.start_time = datetime.now()
        self.agg_mpc_data = self.set_agg_mpc_initial_vals()
        for t in range(self.num_timesteps):
            self.converged = False
            while True:
                for home in self.all_homes:
                    if self.check_type == "all" or home["type"] == self.check_type:
                        self.queue.put(home)
                self.redis_set_current_values()
                self.run_iteration(horizon)
                self.check_agg_mpc_data()
                self.update_agg_mpc_data()
                if self.converged:
                    self.agg_log.logger.info(f"Converged for ts: {self.timestep} after iter: {self.iteration}")
                    break
                self.reward_price = self.update_reward_price()
                self.iteration += 1
                self.agg_log.logger.info(f"Not converged for ts: {self.timestep}; iter: {self.iteration}; rp: {self.reward_price:.20f}")
                # time.sleep(5)
                if hour > 0:
                    self.redis_set_state_for_previous_timestep()
            self.collect_data()
            self.iteration = 0
            self.reward_price = 0
            self.timestep += 1
        # Write
        self.end_time = datetime.now()
        self.t_diff = self.end_time - self.start_time
        self.agg_log.logger.info(f"Horizon: {horizon}; Num Hours Simulated: {self.hours}; Run time: {self.t_diff.total_seconds()} seconds")
        self.check_baseline_vals()

    def set_rl_q_initial_vals(self):
        temp = {}
        temp["timestep"] = [-1]
        temp["theta"] = []
        temp["phi"] = []
        temp["q_obs"] = []
        temp["q_pred"] = []
        temp["action"] = []
        temp["is_greedy"] = []
        temp["q_tables"] = []
        temp["average_reward"] = []
        temp["cumulative_reward"] = []
        temp["reward"] = []
        return temp

    def _gen_forecast(self):
        for home in self.all_homes:
             if self.check_type == "all" or home["type"] == self.check_type:
                 self.queue.put(home)

        forecast_horizon = self.config['rl']['utility']['rl_agg_forecast_horizon']
        if forecast_horizon < 1:
            forecast = self.agg_load * np.ones(self.horizon)
        else:
            forecast = []
            for t in range(forecast_horizon):
                worker = MPCCalc(self.queue, self.horizon, self.dt, self.mpc_discomfort, self.mpc_disutility, self.case, self.redis_client, self.forecast_log)
                forecast.append(worker.forecast(0)) # optionally give .forecast() method an expected value for the next RP
        return forecast # returns all forecast values in the horizon

    def _gen_setpoint(self, time):
        """ @kyri: setpoint of community """
        sp = self.config['community']['total_number_homes']*2.5 # increased for homes with plug load
        return sp

    def test_response(self):
        """ @kyri: to be changed for the response rate of the community (see mpc_disutility in new config.json)"""
        c = 0.2
        if self.timestep == 0:
            self.agg_load = self.agg_setpoint + 0.1*self.agg_setpoint
        self.agg_load = max(1, self.agg_load - self.mpc_disutility * self.reward_price[0] * self.agg_load) # can't go negative
        self.agg_load = min(self.agg_load, 50)
        if self.reward_price[0] >= -0.02 and self.reward_price[0] <= 0.02:
            self.agg_load = self.agg_load + 0.5*(self.agg_setpoint - self.agg_load)
        #self.agg_load = max(200,self.agg_load) # can't go above 200
        self.agg_cost = self.agg_load * self.reward_price[0]

    def run_rl_agg(self, horizon):

        self.agg_log.logger.info(f"Performing RL AGG (agg. horizon: {self.rl_agg_horizon}, learning rate: {self.alpha}, discount factor: {self.beta}, exploration rate: {self.epsilon}) with MPC HEMS for horizon: {self.horizon}")
        self.start_time = datetime.now()
        self.rl_q_data = self.set_rl_q_initial_vals()
        # self.forecast_data = self.rl_initialize_forecast()
        self.baseline_agg_load_list = [0]

        self.forecast_load = self._gen_forecast()
        self.prev_forecast_load = self.forecast_load
        self.forecast_setpoint = self._gen_setpoint(self.timestep)
        self.agg_load = self.forecast_load[0] # approximate load for initial timestep
        self.agg_setpoint = self._gen_setpoint(self.timestep)
        self.action = 0
        self.prev_action = 0
        self.state = self._calc_state()
        # self.timestep += 1

        self.is_greedy=True
        n = len(self._phi(self.state, self.action))
        self.theta = -1.0*np.ones(n) # theta initialization
        self.theta_a = np.random.normal(-1, 0.3, n)
        self.theta_b = np.random.normal(-1, 0.3, n)
        self.cumulative_reward = 0
        self.average_reward = 0
        n = len(self._state_basis(self.state))
        self.theta_mu = np.zeros(n) # theta initialization
        self.theta_sigma = np.zeros(n)
        self.lam_w = 0.01
        self.lam_theta = 0.01
        self.alpha_theta = 2 ** -9
        self.alpha_w = 2 ** -6
        self.alpha_r = 2 ** -3
        self.w = np.zeros(n)
        self.z_w = 0
        self.z_theta_mu = 0
        self.z_theta_sigma = 0
        self.tau = 0.005

        for t in range(self.num_timesteps):
            self.agg_setpoint = self._gen_setpoint(self.timestep // self.dt)
            self.prev_forecast_load = self.forecast_load
            self.forecast_load = [self.agg_load] # forecast current load at next timestep
            self.forecast_setpoint = self._gen_setpoint(self.timestep + 1)

            self.rl_update_reward_price()
            self.redis_set_current_values() # broadcast rl price to community

            for home in self.all_homes: # uncomment these for the actual model response
                 if self.check_type == "all" or home["type"] == self.check_type:
                     self.queue.put(home)
            self.run_iteration(horizon) # community response to broadcasted price (done in a single iteration)
            self.collect_data()

            self.next_state = self._calc_state() # this is the state at t = k+1
            self.reward = self._reward(self.state)
            self.cumulative_reward += self.reward

            if self.timestep % 2 == 0:
                self.theta = self.theta_a
            else:
                self.theta = self.theta_b
            self.next_greedy_action = self._get_greedyaction(self.next_state) # necessary for SARSA learning
            self.next_action = self._get_policyaction(self.next_state)
            if self.timestep % 2 == 0:
                self.theta_a = self.update_qfunction(self.theta)
            else:
                self.theta_b = self.update_qfunction(self.theta)
            self.record_rl_q_data()

            self._experience()
            self.timestep += 1
            self.state = self.next_state
            self.action = self.next_action

        self.end_time = datetime.now()
        self.t_diff = self.end_time - self.start_time
        self.agg_log.logger.info(f"Horizon: {horizon}; Num Hours Simulated: {self.hours}; Run time: {self.t_diff.total_seconds()} seconds")

    def run_rl_agg_simplified(self):
        self.agg_log.logger.info(f"Performing RL AGG (agg. horizon: {self.rl_agg_horizon}, learning rate: {self.alpha}, discount factor: {self.beta}, exploration rate: {self.epsilon}) with simplified community model.")
        self.start_time = datetime.now()
        self.rl_agg_data = self.set_rl_agg_initial_vals()
        self.rl_q_data = self.set_rl_q_initial_vals()
        # self.forecast_data = self.rl_initialize_forecast()
        self.baseline_agg_load_list = [0]

        self.forecast_setpoint = self._gen_setpoint(self.timestep)
        self.forecast_load = [self.forecast_setpoint]
        self.prev_forecast_load = self.forecast_load

        self.agg_load = self.forecast_load[0] # approximate load for initial timestep
        self.agg_setpoint = self._gen_setpoint(self.timestep)
        self.action = 0
        self.prev_action = 0
        self.state = self._calc_state()
        # self.timestep += 1

        self.is_greedy=True
        n = len(self._phi(self.state, self.action))
        self.theta = -1*np.ones(n) # theta initialization
        self.theta_a = np.random.normal(-1, 0.3, n)
        self.theta_b = np.random.normal(-1, 0.3, n)
        self.cumulative_reward = 0
        self.average_reward = 0

        for t in range(self.num_timesteps):
            self.agg_setpoint = self._gen_setpoint(self.timestep // self.dt)
            self.prev_forecast_load = self.forecast_load
            self.forecast_load = [self.agg_load] # forecast current load at next timestep
            self.forecast_setpoint = self._gen_setpoint(self.timestep + 1)

            self.rl_update_reward_price()
            self.redis_set_current_values() # broadcast rl price to community
            self.test_response()
            self.baseline_agg_load_list.append(self.agg_load)

            self.next_state = self._calc_state() # this is the state at t = k+1
            self.reward = self._reward(self.state)
            self.cumulative_reward += self.reward

            if self.timestep % 2 == 0:
                self.theta = self.theta_a
            else:
                self.theta = self.theta_b
            self.next_greedy_action = self._get_greedyaction(self.next_state) # necessary for SARSA learning
            self.next_action = self._get_action(self.next_state)
            if self.timestep % 2 == 0:
                self.theta_a = self.update_qfunction(self.theta)
            else:
                self.theta_b = self.update_qfunction(self.theta)
            self.record_rl_q_data()

            self._experience()
            self.timestep += 1
            self.state = self.next_state
            self.action = self.next_action

        self.end_time = datetime.now()
        self.t_diff = self.end_time - self.start_time
        self.agg_log.logger.info(f"Num Hours Simulated: {self.hours}; Run time: {self.t_diff.total_seconds()} seconds")

    def flush_redis(self):
        self.redis_client.conn.flushall()
        self.agg_log.logger.info("Flushing Redis")
        time.sleep(1)
        self.check_all_data_indices()
        self.calc_start_hour_index()
        self.redis_add_all_data()

    def run(self):
        self.agg_log.logger.info("Made it to Aggregator Run")
        self.create_homes()
        self.write_home_configs()

        if self.config['simulation']['run_rbo_mpc']:
            # Run baseline MPC with N hour horizon, no aggregator
            # Run baseline with 1 hour horizon for non-MPC HEMS
            self.case = "baseline" # no aggregator
            for h in self.config['home']['hems']['prediction_horizon']:
                for c in self.config['home']['hems']['discomfort']:
                    self.mpc_discomfort = float(c)
                    for u in self.config['home']['hems']['disutility']:
                        self.mpc_disutility = float(u)
                        self.flush_redis()
                        self.redis_set_initial_values()
                        self.reset_baseline_data()
                        self.run_baseline(h)
                        self.summarize_baseline(h)
                        self.write_outputs(h)

        # if self.config["run_agg_mpc"]:
        #     self.case = "agg_mpc"
        #     self.horizon = self.config["agg_mpc_horizon"]
        #     for threshold in self.config["max_load_threshold"]:
        #         self.max_load_threshold = threshold
        #         self.flush_redis()
        #         self.redis_set_initial_values()
        #         self.reset_baseline_data()
        #         self.run_agg_mpc(self.horizon)
        #         self.summarize_baseline(self.horizon)
        #         self.write_outputs(self.horizon)

        if self.config['simulation']['run_rl_agg']:
            self.case = "rl_agg"

            for h in self.config['home']['hems']['prediction_horizon']:
                self.horizon = int(h)
                for a in self.config['rl']['parameters']['learning_rate']:
                    self.alpha = float(a)
                    for b in self.config['rl']['parameters']['discount_factor']:
                        self.beta = float(b)
                        for e in self.config['rl']['parameters']['exploration_rate']:
                            self.epsilon_init = float(e)
                            self.epsilon = self.epsilon_init
                            for rl_h in self.config['rl']['utility']['rl_agg_action_horizon']:
                                self.rl_agg_horizon = int(rl_h)
                                for bs in self.config['rl']['parameters']['batch_size']:
                                    self.batch_size = int(bs)
                                    for md in self.config['home']['hems']['disutility']:
                                        self.mpc_disutility = float(md)
                                        for discomfort in self.config['home']['hems']['discomfort']:
                                            self.mpc_discomfort = float(discomfort)
                                            self.flush_redis()
                                            self.redis_set_initial_values()
                                            self.reset_baseline_data()
                                            self.run_rl_agg(self.horizon)
                                            self.summarize_baseline(self.horizon)
                                            self.write_outputs(self.horizon)

        if self.config['simulation']['run_rl_simplified']:
            self.case = "simplified"
            self.horizon = self.config['home']['hems']['prediction_horizon'][0] # arbitrary

            for a in self.config['rl']['parameters']['learning_rate']:
                self.alpha = float(a)
                for b in self.config['rl']['parameters']['discount_factor']:
                    self.beta = float(b)
                    for e in self.config['rl']['parameters']['exploration_rate']:
                        self.epsilon_init = float(e)
                        self.epsilon = self.epsilon_init
                        for rl_h in self.config['rl']['utility']['rl_agg_action_horizon']:
                            self.rl_agg_horizon = int(rl_h)
                            for bs in self.config['rl']['parameters']['batch_size']:
                                self.batch_size = int(bs)
                                for md in self.config['home']['hems']['disutility']:
                                    self.mpc_disutility = float(md)
                                    self.flush_redis()
                                    self.redis_set_initial_values()
                                    self.reset_baseline_data()
                                    self.run_rl_agg_simplified()
                                    self.summarize_baseline(self.horizon)
                                    self.write_outputs(self.horizon)

    # function graveyard

    def experience_replay(self):
        num_memories = len(self.memory)
        if num_memories > self.batch_size: # experience replay
            temp_theta = deepcopy(self.theta)
            batch = random.sample(self.memory, self.batch_size)
            for experience in batch:
                temp_theta = self.update_theta(temp_theta, experience)
            if num_memories > self.memory_size:
                self.memory.remove(self.memory[0]) # remove oldest memory
        else:
            temp_theta = self.theta
        return temp_theta

    def check_agg_mpc_data(self):
        self.agg_load = 0
        self.agg_cost = 0
        for home in self.all_homes:
            if self.check_type == 'all' or home["type"] == self.check_type:
                vals = self.redis_client.conn.hgetall(home["name"])
                self.agg_load += float(vals["p_grid_opt"])
                self.agg_cost += float(vals["cost_opt"])
        self.marginal_demand = max(self.agg_load - self.max_load_threshold, 0)
        self.agg_log.logger.info(f"Aggregate Load: {self.agg_load:.20f}")
        self.agg_log.logger.info(f"Max Threshold: {self.max_load_threshold:.20f}")
        self.agg_log.logger.info(f"Marginal Demand: {self.marginal_demand:.20f}")
        if self.marginal_demand == 0:
            self.converged = True

    def update_agg_mpc_data(self):
        self.agg_mpc_data[self.timestep]["reward_price"].append(self.reward_price)
        self.agg_mpc_data[self.timestep]["agg_cost"].append(self.agg_cost)
        self.agg_mpc_data[self.timestep]["agg_load"].append(self.agg_load)

    def update_reward_price(self):
        rp = self.reward_price + self.step_size_coeff * self.marginal_demand
        return rp
