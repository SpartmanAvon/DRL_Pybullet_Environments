import os
import time
import json
import numpy as np
import pickle

class Logger:
    """
    A general-purpose logger.
    Simplify the saving of diagnostics, hyperparameter configurations, and the 
    state of a training run. Saves the data in the form of a dictionary, and dumps them into a .json file
    """
    def __init__(self, output_dir=None, output_fname='progress.pickle'):
        """
        Initialize a Logger.
        Args:
            output_dir (string): A directory for saving results to. If 
                ``None``, defaults to a temp directory of the form
                ``tmp/experiments/somerandomnumber``.
            output_fname (string): Name for the tab-separated-value file 
                containing metrics logged throughout a training run. 
                Defaults to ``progress.txt``. 
        """
        self.output_dir = output_dir or os.path.join("tmp", "experiments", f"{int(time.time())}")
        os.makedirs(self.output_dir, exist_ok=True)

        self.output_filepath = os.path.join(self.output_dir, output_fname)
        self.logger_dict = {}
        if os.path.isfile(self.output_filepath):
            with open(self.output_filepath, 'rb') as f:
                self.logger_dict = pickle.load(f)
        

    def store(self, **kwargs):
        """
        Save something into the logger's current state.
        Provide an arbitrary number of keyword arguments with numerical 
        values.
        """
        for k, v in kwargs.items():
            if not(k in self.logger_dict.keys()):
                self.logger_dict[k] = []
            self.logger_dict[k].append(v)

    def dump(self):
        """
        Write all of the diagnostics from the current iteration.
        Writes to the output file.
        """
        # print(self.logger_dict)
        with open(self.output_filepath, 'wb') as f:
            pickle.dump(self.logger_dict, f)

    def load_results(self, keys):
        '''
        return all the stored variables in the .json file
        Args:
            keys (list): list of keys to extract from logger
        '''
        output = []
        for key in keys:
            assert key in self.logger_dict.keys(), "Attempted to get variables that are not stored in this .json file"
            output.append(self.logger_dict[key])
        return output