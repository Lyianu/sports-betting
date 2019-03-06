"""
Includes classes and functions to test and select the optimal 
betting strategy on historical and current data.
"""

# Author: Georgios Douzas <gdouzas@icloud.com>
# License: BSD 3 clause

from argparse import ArgumentParser
from ast import literal_eval
from itertools import product
from os.path import join
from pickle import dump, load
from sqlite3 import connect

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.model_selection import BaseCrossValidator, ParameterGrid
from sklearn.utils import Parallel, check_random_state, delayed
from sklearn.utils.metaestimators import _BaseComposition
from tqdm import tqdm

from config import PORTOFOLIOS
from sportsbet import SOCCER_PATH
from sportsbet.soccer import TARGET_TYPES_MAPPING

DB_CONNECTION = connect(join(SOCCER_PATH, 'soccer.db'))


def calculate_yields(y, y_pred, odds, target_types, calibration):

    # Extract targets
    y = np.column_stack([TARGET_TYPES_MAPPING[target_type][0](y) for target_type in target_types])

    # Calculate yields
    yields = y * odds - 1.0

    # Calculate edges
    edges = y_pred - np.array(calibration)

    # Apply calibration
    yields[edges <= 0.0] = 0.0
    yields = yields[range(len(yields)), edges.argmax(axis=1)]

    # Exclude no bets
    mask = yields != 0.0
    yields = yields[mask]

    return yields, mask.mean()


def fit_predict(clf, X, y, train_indices, test_indices):
    """Fit classifier to training data and predict test data."""
    y_pred = clf.fit(X.iloc[train_indices], y.iloc[train_indices]).predict_proba(X.iloc[test_indices])
    return y_pred


def check_random_states(random_state, repetitions):
    """Create random states for experiments."""
    random_state = check_random_state(random_state)
    return [random_state.randint(0, 2 ** 32 - 1, dtype='uint32') for _ in range(repetitions)]


def apply_backtesting(classifiers, X, y, odds, cv, random_state, n_runs):
    """Apply backtesting to betting classifiers."""

    # Check random states
    random_states = check_random_states(random_state, n_runs)

    # Unpack classifiers
    target_types = [target_type for target_type, *_, in classifiers]
    clfs = [clone(clf) for _, clf, *_ in classifiers]
    param_grids_combinations = product(*[list(ParameterGrid(param_grid)) for _, _, param_grid, *_ in classifiers])
    calibrations = list(product(*[calibration for *_, calibration, _ in classifiers]))
    features_container = [features for *_, features in classifiers]
    
    # Stack input and odds data
    X = pd.concat([X, odds[target_types]], axis=1)

    # Extract test targets
    y_test = pd.concat([y.iloc[test_indices] for _, test_indices in cv.split()])

    backtesting_results = []
    for random_state, param_grids in tqdm(list(product(random_states, param_grids_combinations)), desc='Fitting tasks'):
        
        # Define betting classifiers
        betting_classifiers = []
        for target_type, features, clf, param_grid in zip(target_types, features_container, clfs, param_grids):
            for param in clf.get_params():
                if 'random_state' in param:
                    clf.set_params(**{param: random_state})
            betting_classifiers.append((target_type, features, BettingClassifier(clf.set_params(**param_grid))))
    
        # Define metabetting classifier
        mbclf = _MetaBettingClassifier(betting_classifiers)

        # Extract test target values, predictions and odds
        y_pred, odds = zip(*Parallel(n_jobs=-1)(delayed(fit_predict)(mbclf, X, y, train_indices, test_indices) for train_indices, test_indices in cv.split()))

        # Backtesting results
        for calibration in calibrations:
            yields, coverage = calculate_yields(y_test, np.row_stack(y_pred), np.row_stack(odds), target_types, calibration)
            backtesting_results.append([random_state, str(param_grids), str(calibration), yields.mean(), yields.std(), coverage])

    # Aggregate and format backtesting results
    backtesting_results = pd.DataFrame(backtesting_results, columns=['experiment', 'parameters', 'calibration', 'mean_yield', 'std_yield', 'coverage'])
    backtesting_results = backtesting_results.groupby(['parameters', 'calibration'], as_index=False).agg({'mean_yield': [np.mean, np.std], 'std_yield': np.mean, 'coverage': np.mean})
    backtesting_results.columns = ['parameters', 'calibration', 'mean_yield', 'std_mean_yield', 'std_yield', 'coverage']
    backtesting_results = backtesting_results.sort_values('mean_yield', ascending=False).reset_index(drop=True)
    
    return backtesting_results


class SeasonSplit(BaseCrossValidator):
    """Split time-series data based on a test season."""

    def __init__(self, n_splits, seasons, test_season):
        self.n_splits = n_splits
        self.seasons = seasons
        self.test_season = test_season

    def split(self, X=None, y=None, groups=None):
        """Generate indices to split data into training and test set."""
        start_index, end_index = (self.seasons != self.test_season).sum(), len(self.seasons)
        step = (end_index - start_index) // self.n_splits
        breakpoints = list(range(start_index, end_index, step)) + [end_index]
        for start, end in zip(breakpoints[:-1], breakpoints[1:]):
            yield np.arange(0, start), np.arange(start, end)

    def get_n_splits(self, X=None, y=None, groups=None):
        """Returns the number of splitting iterations in the cross-validator"""
        return self.n_splits


class BettingClassifier(BaseEstimator, ClassifierMixin):

    def __init__(self, classifier):
        self.classifier = classifier

    def fit(self, X, y, target_type):
        """Fit betting classifier."""

        # Extract target
        y = TARGET_TYPES_MAPPING[target_type][1](y)

        # Fit classifier
        self.classifier_ = clone(self.classifier).fit(X.iloc[:, :-1], y)

        return self

    def predict_proba(self, X):
        """"Predict probability of betting classifier."""

        # Predict probability
        y_pred = self.classifier_.predict_proba(X.iloc[:, :-1].values)[:, 1:]

        # Extract odds
        odds = X.iloc[:, -1:].values

        return y_pred, odds


class _MetaBettingClassifier(_BaseComposition):

    def __init__(self, betting_classifiers):
        self.betting_classifiers = betting_classifiers

    def fit(self, X, y):
        """Fit betting classifiers."""

        # Check target types
        target_types = [target_type for target_type, *_, in self.betting_classifiers]
        if not set(target_types).issubset(TARGET_TYPES_MAPPING):
            raise ValueError('Selected target types are not supported.')

        # Check features
        features_container = [features for _, features, _ in self.betting_classifiers]
        for features in features_container:
            if not set(features).issubset(X.columns):
                raise ValueError('Selected features are not included in the dataset.')
        
        # Placeholder
        self.betting_classifiers_ = []

        # Number of classifiers
        self.n_clfs_ = len(self.betting_classifiers)

        # Fit betting classifiers
        for ind, (target_type, features, clf) in enumerate(self.betting_classifiers):
            X_clf = pd.concat([X[features], X.iloc[:, -self.n_clfs_:]], axis=1)
            X_clf = pd.concat([X_clf.iloc[:, :-self.n_clfs_], X_clf.iloc[:, ind - self.n_clfs_]], axis=1)
            self.betting_classifiers_.append((features, clone(clf).fit(X_clf, y, target_type)))

        return self
    
    def predict_proba(self, X):
        """Predict probabilities of betting classifiers."""

        # Placeholders
        y_pred, odds = [], []

        # Predict probabilities
        for ind, (features, clf) in enumerate(self.betting_classifiers_):
            X_clf = pd.concat([X[features], X.iloc[:, -self.n_clfs_:]], axis=1)
            X_clf = pd.concat([X_clf.iloc[:, :-self.n_clfs_], X_clf.iloc[:, ind - self.n_clfs_]], axis=1)
            proba, odd = clf.predict_proba(X_clf)
            y_pred.append(proba)
            odds.append(odd)
        
        return np.column_stack(y_pred), np.column_stack(odds)


def backtest():
    """Command line function to backtest models.""" 

    # Create parser
    parser = ArgumentParser('Models evaluation using backtesting.')
        
    # Add arguments
    parser.add_argument('portofolio', help='The name of portofolio to evaluate.')
    parser.add_argument('--test-season', default='1819', type=str, help='The test season.')
    parser.add_argument('--n-splits', default=5, type=int, help='Number of cross-validation splits.')
    parser.add_argument('--random-state', default=0, type=int, help='The random seed.')
    parser.add_argument('--n-runs', default=5, type=int, help='Number of evaluation runs.')

    # Parse arguments
    args = parser.parse_args()
    
    # Load data
    X = pd.read_sql('select * from X', DB_CONNECTION)
    y = pd.read_sql('select * from y', DB_CONNECTION)
    odds = pd.read_sql('select * from odds', DB_CONNECTION)

    # Create cross-validator
    cv = SeasonSplit(args.n_splits, X['season'].values, args.test_season)

    # Backtesting
    results = apply_backtesting(PORTOFOLIOS[args.portofolio], X, y, odds, cv, args.random_state, args.n_runs)
    results['portofolio'] = args.portofolio

    # Save backtesting results
    try:
        backtesting_results = pd.read_sql('select * from backtesting_results', DB_CONNECTION)
        backtesting_results = backtesting_results[backtesting_results['portofolio'] != args.portofolio]
    except pd.io.sql.DatabaseError:
        backtesting_results = pd.DataFrame([])
    backtesting_results = backtesting_results.append(results, ignore_index=True).sort_values('mean_yield', ascending=False)
    backtesting_results.to_sql('backtesting_results', DB_CONNECTION, index=False, if_exists='replace')


def predict():
    """Command line function to predict new fixtures.""" 

    # Create parser
    parser = ArgumentParser('Predict new fixtures.')
        
    # Add arguments
    parser.add_argument('portofolio', help='The name of portofolio to evaluate.')
    parser.add_argument('--rank', default=0, type=int, help='The rank of the model to use for predictions.')

    # Parse arguments
    args = parser.parse_args()

    # Load data
    X = pd.read_sql('select * from X', DB_CONNECTION)
    y = pd.read_sql('select * from y', DB_CONNECTION)
    odds = pd.read_sql('select * from odds', DB_CONNECTION)
    X_test = pd.read_sql('select * from X_test', DB_CONNECTION)
    odds_test = pd.read_sql('select * from odds_test', DB_CONNECTION)
    parameters, calibration = pd.read_sql('select parameters, calibration from backtesting_results where portofolio == "{}"'.format(args.portofolio), DB_CONNECTION).values[args.rank]

    # Stack input and odds data
    target_types = [target_type for target_type, *_, in PORTOFOLIOS[args.portofolio]]
    X = pd.concat([X, odds[target_types]], axis=1)
    X_test = pd.concat([X_test, odds_test[target_types]], axis=1)

    # Fit betting classifiers
    betting_classifiers = [(target_type, features, BettingClassifier(clf.set_params(**params))) for params, (target_type, clf, *_, features) in zip(literal_eval(parameters), PORTOFOLIOS[args.portofolio])]
    mbclf = _MetaBettingClassifier(betting_classifiers).fit(X, y)
    
    # Get predictions
    y_pred = mbclf.predict_proba(X_test)[0]
    bets_indices = y_pred.argmax(axis=1)
    mask = (y_pred > literal_eval(calibration))[range(len(y_pred)), bets_indices]

    # Format predictions
    predictions = X_test.loc[:, ['date', 'league', 'team1', 'team2']]
    predictions['odd'] = odds_test[target_types].values[range(len(y_pred)), bets_indices]
    predictions['bet'] = np.array(target_types)[bets_indices]
    predictions = predictions[mask]

    # Save predictions
    predictions.to_csv(join(SOCCER_PATH, 'predictions.csv'), index=False)