# -*- coding:utf-8 -*-
__author__ = 'yangjian'

"""

"""
import copy
import inspect
import pickle

import numpy as np
import pandas as pd
from IPython.display import display, display_markdown
from sklearn.base import BaseEstimator
from sklearn.metrics import get_scorer
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from hypernets.experiment import Experiment
from hypernets.tabular import dask_ex as dex
from hypernets.tabular import drift_detection as dd
from hypernets.tabular.data_cleaner import DataCleaner
from hypernets.tabular.ensemble import GreedyEnsemble, DaskGreedyEnsemble
from hypernets.tabular.feature_importance import feature_importance_batch, select_by_feature_importance
from hypernets.tabular.feature_selection import select_by_multicollinearity
from hypernets.tabular.general import general_estimator, general_preprocessor
from hypernets.tabular.lifelong_learning import select_valid_oof
from hypernets.tabular.pseudo_labeling import sample_by_pseudo_labeling
from hypernets.utils import hash_data, logging, fs, isnotebook, const

logger = logging.get_logger(__name__)

DEFAULT_EVAL_SIZE = 0.3

_is_notebook = isnotebook()


def _set_log_level(log_level):
    logging.set_level(log_level)

    # if log_level >= logging.ERROR:
    #     import logging as pylogging
    #     pylogging.basicConfig(level=log_level)


class ExperimentStep(BaseEstimator):
    def __init__(self, experiment, name):
        super(ExperimentStep, self).__init__()

        self.name = name
        self.experiment = experiment

    def step_start(self, *args, **kwargs):
        if self.experiment is not None:
            self.experiment.step_start(*args, **kwargs)

    def step_end(self, *args, **kwargs):
        if self.experiment is not None:
            self.experiment.step_end(*args, **kwargs)

    def step_progress(self, *args, **kwargs):
        if self.experiment is not None:
            self.experiment.step_progress(*args, **kwargs)

    @property
    def task(self):
        return self.experiment.task if self.experiment is not None else None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        raise NotImplemented()
        # return hyper_model, X_train, y_train, X_test, X_eval, y_eval,

    def transform(self, X, y=None, **kwargs):
        raise NotImplemented()
        # return X

    def is_transform_skipped(self):
        return False

    # override this to remove 'experiment' from estimator __expr__
    @classmethod
    def _get_param_names(cls):
        params = super()._get_param_names()
        return filter(lambda x: x != 'experiment', params)

    def __getstate__(self):
        state = super().__getstate__()
        # Don't pickle experiment
        if 'experiment' in state.keys():
            state['experiment'] = None
        return state


def cache_fit(attr_names, keys=('X_train', 'y_train', 'X_test', 'X_eval', 'y_eval'), transform_fn=None):
    assert isinstance(attr_names, (tuple, list, str)) and len(attr_names) > 0
    assert callable(transform_fn) or isinstance(transform_fn, str) or (transform_fn is None)

    if isinstance(attr_names, str):
        attr_names = [a.strip(' ') for a in attr_names.split(',') if len(a.strip(' ')) > 0]

    def decorate(fn):
        def _call(step, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
            assert isinstance(step, ExperimentStep)

            result = None

            all_items = dict(X_train=X_train, y_train=y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                             **kwargs)
            pre_hashed_types = (pd.DataFrame, pd.Series, dex.dd.DataFrame, dex.dd.Series)
            key_items = {k: v if not isinstance(v, pre_hashed_types) else hash_data(v)
                         for k, v in all_items.items() if keys is None or k in keys}
            key_items['step_params'] = step.get_params(deep=False)
            key = hash_data(key_items)

            cache_dir = getattr(hyper_model, 'cache_dir', None)
            if cache_dir is None:
                cache_dir = 'step_cache'
                try:
                    fs.mkdirs(cache_dir, exist_ok=True)
                except:
                    pass
            cache_file = f'{cache_dir}{fs.sep}cache_{step.name}_{key}.pkl'

            # load cache
            try:
                if fs.exists(cache_file):
                    with fs.open(cache_file, 'rb') as f:
                        cached_data = pickle.load(f)
                    for k in attr_names:
                        v = cached_data[k]
                        setattr(step, k, v)

                    if isinstance(transform_fn, str):
                        tfn = getattr(step, transform_fn)
                        result = tfn(hyper_model,
                                     X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                                     **kwargs)
                    elif callable(transform_fn):
                        tfn = transform_fn
                        result = tfn(step, hyper_model,
                                     X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                                     **kwargs)
                    else:
                        result = (X_train, y_train, X_test, X_eval, y_eval)
            except Exception as e:
                logger.warning(e)

            if result is None:
                result = fn(step, hyper_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                            **kwargs)

                try:
                    # store cache
                    cached_data = {k: getattr(step, k) for k in attr_names}
                    if 'keys' not in cached_data:
                        cached_data['keys'] = key_items  # for info

                    with fs.open(cache_file, 'wb') as f:
                        pickle.dump(cached_data, f)
                except Exception as e:
                    logger.warning(e)

            return result

        return _call

    return decorate


class FeatureSelectStep(ExperimentStep):

    def __init__(self, experiment, name):
        super().__init__(experiment, name)

        # fitted
        self.selected_features_ = None

    def transform(self, X, y=None, **kwargs):
        if self.selected_features_ is not None:
            if logger.is_debug_enabled():
                msg = f'{self.name} transform from {len(X.columns.tolist())} to {len(self.selected_features_)} features'
                logger.debug(msg)
            X = X[self.selected_features_]
        return X

    def cache_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if self.selected_features_ is not None:
            features = self.selected_features_
            X_train = X_train[features]
            if X_test is not None:
                X_test = X_test[features]
            if X_eval is not None:
                X_eval = X_eval[features]
            if logger.is_info_enabled():
                logger.info(f'{self.name} cache_transform: {len(X_train.columns)} columns kept.')
        else:
            if logger.is_info_enabled():
                logger.info(f'{self.name} cache_transform: {len(X_train.columns)} columns kept (do nothing).')

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval

    def is_transform_skipped(self):
        return self.selected_features_ is None


class DataCleanStep(ExperimentStep):
    def __init__(self, experiment, name, data_cleaner_args=None,
                 cv=False, train_test_split_strategy=None, random_state=None):
        super().__init__(experiment, name)

        self.data_cleaner_args = data_cleaner_args if data_cleaner_args is not None else {}
        self.cv = cv
        self.train_test_split_strategy = train_test_split_strategy
        self.random_state = random_state

        # fitted
        self.selected_features_ = None
        self.data_cleaner = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        self.step_start('clean and split data')
        # 1. Clean Data
        if self.cv and X_eval is not None and y_eval is not None:
            logger.info(f'{self.name} cv enabled, so concat train data and eval data')
            X_train = dex.concat_df([X_train, X_eval], axis=0)
            y_train = dex.concat_df([y_train, y_eval], axis=0)
            X_eval = None
            y_eval = None

        data_cleaner = DataCleaner(**self.data_cleaner_args)

        logger.info(f'{self.name} fit_transform with train data')
        X_train, y_train = data_cleaner.fit_transform(X_train, y_train)
        self.step_progress('fit_transform train set')

        if X_test is not None:
            logger.info(f'{self.name} transform test data')
            X_test = data_cleaner.transform(X_test)
            self.step_progress('transform X_test')

        if not self.cv:
            if X_eval is None or y_eval is None:
                eval_size = self.experiment.eval_size
                if self.train_test_split_strategy == 'adversarial_validation' and X_test is not None:
                    logger.debug('DriftDetector.train_test_split')
                    detector = dd.DriftDetector()
                    detector.fit(X_train, X_test)
                    X_train, X_eval, y_train, y_eval = detector.train_test_split(X_train, y_train, test_size=eval_size)
                else:
                    if self.task == const.TASK_REGRESSION or dex.is_dask_object(X_train):
                        X_train, X_eval, y_train, y_eval = dex.train_test_split(X_train, y_train, test_size=eval_size,
                                                                                random_state=self.random_state)
                    else:
                        X_train, X_eval, y_train, y_eval = dex.train_test_split(X_train, y_train, test_size=eval_size,
                                                                                random_state=self.random_state,
                                                                                stratify=y_train)
                if self.task != const.TASK_REGRESSION:
                    y_train_uniques = set(y_train.unique()) if hasattr(y_train, 'unique') else set(y_train)
                    y_eval_uniques = set(y_eval.unique()) if hasattr(y_eval, 'unique') else set(y_eval)
                    assert y_train_uniques == y_eval_uniques, \
                        'The classes of `y_train` and `y_eval` must be equal. Try to increase eval_size.'
                self.step_progress('split into train set and eval set')
            else:
                X_eval, y_eval = data_cleaner.transform(X_eval, y_eval)
                self.step_progress('transform eval set')

        self.step_end(output={'X_train.shape': X_train.shape,
                              'y_train.shape': y_train.shape,
                              'X_eval.shape': None if X_eval is None else X_eval.shape,
                              'y_eval.shape': None if y_eval is None else y_eval.shape,
                              'X_test.shape': None if X_test is None else X_test.shape})

        selected_features = X_train.columns.to_list()

        if _is_notebook:
            display_markdown('### Data Cleaner', raw=True)

            display(data_cleaner, display_id='output_cleaner_info1')
            display_markdown('### Train set & Eval set', raw=True)

            display_data = (X_train.shape,
                            y_train.shape,
                            X_eval.shape if X_eval is not None else None,
                            y_eval.shape if y_eval is not None else None,
                            X_test.shape if X_test is not None else None)
            if dex.exist_dask_object(X_train, y_train, X_eval, y_eval, X_test):
                display_data = [dex.compute(shape)[0] for shape in display_data]
            display(pd.DataFrame([display_data],
                                 columns=['X_train.shape',
                                          'y_train.shape',
                                          'X_eval.shape',
                                          'y_eval.shape',
                                          'X_test.shape']), display_id='output_cleaner_info2')
        else:
            logger.info(f'{self.name} keep {len(selected_features)} columns')

        self.selected_features_ = selected_features
        self.data_cleaner = data_cleaner

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval

    def transform(self, X, y=None, **kwargs):
        return self.data_cleaner.transform(X, y, **kwargs)


class MulticollinearityDetectStep(FeatureSelectStep):

    def __init__(self, experiment, name):
        super().__init__(experiment, name)

    @cache_fit('selected_features_', keys='X_train', transform_fn='cache_transform')
    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Drop features with collinearity', raw=True)

        self.step_start('drop features with multicollinearity')
        corr_linkage, remained, dropped = select_by_multicollinearity(X_train)
        self.output_multi_collinearity_ = {
            'corr_linkage': corr_linkage,
            'remained': remained,
            'dropped': dropped
        }
        self.step_progress('calc correlation')

        if dropped:
            self.selected_features_ = remained

            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            self.step_progress('drop features')
        else:
            self.selected_features_ = None
        self.step_end(output=self.output_multi_collinearity_)
        if _is_notebook:
            display(pd.DataFrame([(k, v)
                                  for k, v in self.output_multi_collinearity_.items()],
                                 columns=['key', 'value']),
                    display_id='output_drop_feature_with_collinearity')
        elif logger.is_info_enabled():
            logger.info(f'{self.name} drop {len(dropped)} columns, {len(remained)} kept')

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval


class DriftDetectStep(FeatureSelectStep):

    def __init__(self, experiment, name, remove_shift_variable, variable_shift_threshold,
                 threshold, remove_size, min_features, num_folds):
        super().__init__(experiment, name)

        self.remove_shift_variable = remove_shift_variable
        self.variable_shift_threshold = variable_shift_threshold

        self.threshold = threshold
        self.remove_size = remove_size if 1.0 > remove_size > 0 else 0.1
        self.min_features = min_features if min_features > 1 else 10
        self.num_folds = num_folds if num_folds > 1 else 5

        # fitted
        self.output_drift_detection_ = None

    @cache_fit('selected_features_, output_drift_detection_',
               keys='X_train,X_test', transform_fn='cache_transform')
    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if X_test is not None:
            if _is_notebook:
                display_markdown('### Drift detection', raw=True)

            self.step_start('detect drifting')
            features, history, scores = dd.feature_selection(X_train, X_test,
                                                             remove_shift_variable=self.remove_shift_variable,
                                                             variable_shift_threshold=self.variable_shift_threshold,
                                                             auc_threshold=self.threshold,
                                                             min_features=self.min_features,
                                                             remove_size=self.remove_size,
                                                             cv=self.num_folds)
            dropped = set(X_train.columns.to_list()) - set(features)
            if dropped:
                self.selected_features_ = features
                X_train = X_train[features]
                X_test = X_test[features]
                if X_eval is not None:
                    X_eval = X_eval[features]
            else:
                self.selected_features_ = None

            self.output_drift_detection_ = {'no_drift_features': features, 'history': history}
            self.step_end(output=self.output_drift_detection_)

            if _is_notebook:
                display(pd.DataFrame((('no drift features', features),
                                      ('kept/dropped feature count', f'{len(features)}/{len(dropped)}'),
                                      ('history', history),
                                      ('drift score', scores)),
                                     columns=['key', 'value']), display_id='output_drift_detection')
            elif logger.is_info_enabled():
                logger.info(f'{self.name} drop {len(dropped)} columns, {len(features)} kept')

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval


class FeatureImportanceSelectionStep(FeatureSelectStep):
    def __init__(self, experiment, name, strategy, threshold, quantile, number):
        super(FeatureImportanceSelectionStep, self).__init__(experiment, name)

        self.strategy = strategy
        self.threshold = threshold
        self.quantile = quantile
        self.number = number

        # fitted
        self.features_ = None
        # self.selected_features_ = None # super attribute
        self.unselected_features_ = None
        self.importances_ = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Evaluate feature importance', raw=True)

        self.step_start('evaluate feature importance')

        preprocessor = general_preprocessor(X_train)
        estimator = general_estimator(X_train, task=self.task)
        estimator.fit(preprocessor.fit_transform(X_train, y_train), y_train)
        importances = estimator.feature_importances_
        self.step_progress('training general estimator')

        selected, unselected = \
            select_by_feature_importance(importances, self.strategy,
                                         threshold=self.threshold,
                                         quantile=self.quantile,
                                         number=self.number)

        features = X_train.columns.to_list()
        selected_features = [features[i] for i in selected]
        unselected_features = [features[i] for i in unselected]
        self.step_progress('select by importances')

        if unselected_features:
            X_train = X_train[selected_features]
            if X_eval is not None:
                X_eval = X_eval[selected_features]
            if X_test is not None:
                X_test = X_test[selected_features]

        output_feature_importances_ = {
            'features': features,
            'importances': importances,
            'selected_features': selected_features,
            'unselected_features': unselected_features}

        self.step_progress('drop features')
        self.step_end(output=output_feature_importances_)

        if _is_notebook:
            display_markdown('#### feature selection', raw=True)
            is_selected = [i in selected for i in range(len(importances))]
            df = pd.DataFrame(
                zip(X_train.columns.to_list(), importances, is_selected),
                columns=['feature', 'importance', 'selected'])
            df = df.sort_values('importance', axis=0, ascending=False)
            display(df)
        elif logger.is_info_enabled():
            logger.info(f'{self.name} drop {len(unselected_features)} columns, {len(selected_features)} kept')

        self.features_ = features
        self.selected_features_ = selected_features if len(unselected_features) > 0 else None
        self.unselected_features_ = unselected_features
        self.importances_ = importances

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval


class PermutationImportanceSelectionStep(FeatureSelectStep):

    def __init__(self, experiment, name, scorer, estimator_size, importance_threshold):
        assert scorer is not None

        super().__init__(experiment, name)

        self.scorer = scorer
        self.estimator_size = estimator_size
        self.importance_threshold = importance_threshold

        # fixed
        self.unselected_features_ = None
        self.importances_ = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Evaluate permutation importance', raw=True)

        self.step_start('evaluate permutation importance')

        best_trials = hyper_model.get_top_trials(self.estimator_size)
        estimators = [hyper_model.load_estimator(trial.model_file) for trial in best_trials]
        self.step_progress('load estimators')

        if X_eval is None or y_eval is None:
            importances = feature_importance_batch(estimators, X_train, y_train, self.scorer, n_repeats=5)
        else:
            importances = feature_importance_batch(estimators, X_eval, y_eval, self.scorer, n_repeats=5)

        if _is_notebook:
            display_markdown('#### importances', raw=True)
            display(pd.DataFrame(
                zip(importances['columns'], importances['importances_mean'], importances['importances_std']),
                columns=['feature', 'importance', 'std']))
            display_markdown('#### feature selection', raw=True)

        feature_index = np.argwhere(importances.importances_mean < self.importance_threshold)
        selected_features = [feat for i, feat in enumerate(X_train.columns.to_list()) if i not in feature_index]
        unselected_features = list(set(X_train.columns.to_list()) - set(selected_features))
        self.step_progress('calc importance')

        if unselected_features:
            X_train = X_train[selected_features]
            if X_eval is not None:
                X_eval = X_eval[selected_features]
            if X_test is not None:
                X_test = X_test[selected_features]

        output_feature_importances_ = {
            'importances': importances,
            'selected_features': selected_features,
            'unselected_features': unselected_features}
        self.step_progress('drop features')
        self.step_end(output=output_feature_importances_)

        if _is_notebook:
            display(pd.DataFrame([('Selected', selected_features), ('Unselected', unselected_features)],
                                 columns=['key', 'value']))
        elif logger.is_info_enabled():
            logger.info(f'{self.name} drop {len(unselected_features)} columns, {len(selected_features)} kept')

        self.selected_features_ = selected_features if len(unselected_features) > 0 else None
        self.unselected_features_ = unselected_features
        self.importances_ = importances

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval


class SpaceSearchStep(ExperimentStep):
    def __init__(self, experiment, name, cv=False, num_folds=3):
        super().__init__(experiment, name)

        self.cv = cv
        self.num_folds = num_folds

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Pipeline search', raw=True)

        self.step_start('first stage search')

        if not dex.is_dask_object(X_eval):
            kwargs['eval_set'] = (X_eval, y_eval)

        model = copy.deepcopy(self.experiment.hyper_model)  # copy from original hyper_model instance
        model.search(X_train, y_train, X_eval, y_eval, cv=self.cv, num_folds=self.num_folds, **kwargs)

        self.step_end(output={'best_reward': model.get_best_trial().reward})

        return model, X_train, y_train, X_test, X_eval, y_eval

    def transform(self, X, y=None, **kwargs):
        return X

    def is_transform_skipped(self):
        return True


class DaskSpaceSearchStep(SpaceSearchStep):

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        X_train, y_train, X_test, X_eval, y_eval = \
            [v.persist() if dex.is_dask_object(v) else v for v in (X_train, y_train, X_test, X_eval, y_eval)]

        return super().fit_transform(hyper_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)


class EstimatorBuilderStep(ExperimentStep):
    def __init__(self, experiment, name):
        super().__init__(experiment, name)

        # fitted
        self.estimator_ = None

    def transform(self, X, y=None, **kwargs):
        return X

    def is_transform_skipped(self):
        return True


class EnsembleStep(EstimatorBuilderStep):
    def __init__(self, experiment, name, scorer=None, ensemble_size=7):
        assert ensemble_size > 1
        super().__init__(experiment, name)

        self.scorer = scorer if scorer is not None else get_scorer('neg_log_loss')
        self.ensemble_size = ensemble_size

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Ensemble', raw=True)
        else:
            logger.info('start ensemble')
        self.step_start('ensemble')

        best_trials = hyper_model.get_top_trials(self.ensemble_size)
        estimators = [hyper_model.load_estimator(trial.model_file) for trial in best_trials]
        ensemble = self.get_ensemble(estimators, X_train, y_train)

        if all(['oof' in trial.memo.keys() for trial in best_trials]):
            logger.info('ensemble with oofs')
            oofs = self.get_ensemble_predictions(best_trials, ensemble)
            assert oofs is not None
            if hasattr(oofs, 'shape'):
                y_, oofs_ = select_valid_oof(y_train, oofs)
                ensemble.fit(None, y_, oofs_)
            else:
                ensemble.fit(None, y_train, oofs)
        else:
            ensemble.fit(X_eval, y_eval)

        self.estimator_ = ensemble
        self.step_end(output={'ensemble': ensemble})

        if _is_notebook:
            display(ensemble)
        else:
            logger.info(f'ensemble info: {ensemble}')

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval

    def get_ensemble(self, estimators, X_train, y_train):
        return GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)

    def get_ensemble_predictions(self, trials, ensemble):
        oofs = None
        for i, trial in enumerate(trials):
            if 'oof' in trial.memo.keys():
                oof = trial.memo['oof']
                if oofs is None:
                    if len(oof.shape) == 1:
                        oofs = np.zeros((oof.shape[0], len(trials)), dtype=np.float64)
                    else:
                        oofs = np.zeros((oof.shape[0], len(trials), oof.shape[-1]), dtype=np.float64)
                oofs[:, i] = oof

        return oofs


class DaskEnsembleStep(EnsembleStep):
    def get_ensemble(self, estimators, X_train, y_train):
        if dex.exist_dask_object(X_train, y_train):
            predict_kwargs = {}
            if all(['use_cache' in inspect.signature(est.predict).parameters.keys()
                    for est in estimators]):
                predict_kwargs['use_cache'] = False
            return DaskGreedyEnsemble(self.task, estimators, scoring=self.scorer,
                                      ensemble_size=self.ensemble_size,
                                      predict_kwargs=predict_kwargs)

        return super().get_ensemble(estimators, X_train, y_train)

    def get_ensemble_predictions(self, trials, ensemble):
        if isinstance(ensemble, DaskGreedyEnsemble):
            oofs = [trial.memo.get('oof') for trial in trials]
            return oofs if any([oof is not None for oof in oofs]) else None

        return super().get_ensemble_predictions(trials, ensemble)


class FinalTrainStep(EstimatorBuilderStep):
    def __init__(self, experiment, name, retrain_on_wholedata=False):
        super().__init__(experiment, name)

        self.retrain_on_wholedata = retrain_on_wholedata

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Load best estimator', raw=True)

        self.step_start('load estimator')
        if self.retrain_on_wholedata:
            if _is_notebook:
                display_markdown('#### retrain on whole data', raw=True)
            trial = hyper_model.get_best_trial()
            X_all = dex.concat_df([X_train, X_eval], axis=0)
            y_all = dex.concat_df([y_train, y_eval], axis=0)
            estimator = hyper_model.final_train(trial.space_sample, X_all, y_all, **kwargs)
        else:
            estimator = hyper_model.load_estimator(hyper_model.get_best_trial().model_file)

        self.estimator_ = estimator
        display(estimator)
        self.step_end()

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval


class PseudoLabelStep(ExperimentStep):
    def __init__(self, experiment, name, estimator_builder,
                 strategy=None, proba_threshold=None, proba_quantile=None, sample_number=None,
                 resplit=False, random_state=None):
        super().__init__(experiment, name)
        assert hasattr(estimator_builder, 'estimator_')

        self.estimator_builder = estimator_builder
        self.strategy = strategy
        self.proba_threshold = proba_threshold
        self.proba_quantile = proba_quantile
        self.sample_number = sample_number
        self.resplit = resplit
        self.random_state = random_state

    def transform(self, X, y=None, **kwargs):
        return X

    def is_transform_skipped(self):
        return True

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        # build estimator
        hyper_model, X_train, y_train, X_test, X_eval, y_eval = \
            self.estimator_builder.fit_transform(hyper_model, X_train, y_train, X_test=X_test,
                                                 X_eval=X_eval, y_eval=y_eval, **kwargs)
        estimator = self.estimator_builder.estimator_

        # start here
        if _is_notebook:
            display_markdown('### Pseudo_label', raw=True)

        self.step_start('pseudo_label')

        X_pseudo = None
        y_pseudo = None
        if self.task in [const.TASK_BINARY, const.TASK_MULTICLASS] and X_test is not None:
            proba = estimator.predict_proba(X_test)
            proba_threshold = self.proba_threshold
            X_pseudo, y_pseudo = sample_by_pseudo_labeling(X_test, estimator.classes_, proba,
                                                           strategy=self.strategy,
                                                           threshold=self.proba_threshold,
                                                           quantile=self.proba_quantile,
                                                           number=self.sample_number,
                                                           )

            if _is_notebook:
                display_markdown('### Pseudo label set', raw=True)
                display(pd.DataFrame([(dex.compute(X_pseudo.shape)[0],
                                       dex.compute(y_pseudo.shape)[0],
                                       # len(positive),
                                       # len(negative),
                                       proba_threshold)],
                                     columns=['X_pseudo.shape',
                                              'y_pseudo.shape',
                                              # 'positive samples',
                                              # 'negative samples',
                                              'proba threshold']), display_id='output_presudo_labelings')
            try:
                if _is_notebook:
                    import seaborn as sns
                    import matplotlib.pyplot as plt
                    # Draw Plot
                    plt.figure(figsize=(8, 4), dpi=80)
                    sns.kdeplot(proba, shade=True, color="g", label="Proba", alpha=.7, bw_adjust=0.01)
                    # Decoration
                    plt.title('Density Plot of Probability', fontsize=22)
                    plt.legend()
                    plt.show()
                # else:
                #     print(proba)
            except:
                print(proba)

        if X_pseudo is not None:
            X_train, y_train, X_eval, y_eval = \
                self.merge_pseudo_label(X_train, y_train, X_eval, y_eval, X_pseudo, y_pseudo)
            if _is_notebook:
                display_markdown('#### Pseudo labeled train set & eval set', raw=True)
                display(pd.DataFrame([(X_train.shape,
                                       y_train.shape,
                                       X_eval.shape if X_eval is not None else None,
                                       y_eval.shape if y_eval is not None else None,
                                       X_test.shape if X_test is not None else None)],
                                     columns=['X_train.shape',
                                              'y_train.shape',
                                              'X_eval.shape',
                                              'y_eval.shape',
                                              'X_test.shape']), display_id='output_cleaner_info2')

        self.step_end(output={'pseudo_label': 'done'})

        return hyper_model, X_train, y_train, X_test, X_eval, y_eval

    def merge_pseudo_label(self, X_train, y_train, X_eval, y_eval, X_pseudo, y_pseudo, **kwargs):
        if self.resplit:
            x_list = [X_train, X_pseudo]
            y_list = [y_train, pd.Series(y_pseudo)]
            if X_eval is not None and y_eval is not None:
                x_list.append(X_eval)
                y_list.append(y_eval)
            X_mix = pd.concat(x_list, axis=0, ignore_index=True)
            y_mix = pd.concat(y_list, axis=0, ignore_index=True)
            if y_mix.dtype != y_train.dtype:
                y_mix = y_mix.astype(y_train.dtype)
            if self.task == const.TASK_REGRESSION:
                stratify = None
            else:
                stratify = y_mix

            eval_size = self.experiment.eval_size
            X_train, X_eval, y_train, y_eval = \
                train_test_split(X_mix, y_mix, test_size=eval_size,
                                 random_state=self.random_state, stratify=stratify)
        else:
            X_train = pd.concat([X_train, X_pseudo], axis=0)
            y_train = pd.concat([y_train, pd.Series(y_pseudo)], axis=0)

        return X_train, y_train, X_eval, y_eval


class DaskPseudoLabelStep(PseudoLabelStep):
    def merge_pseudo_label(self, X_train, y_train, X_eval, y_eval, X_pseudo, y_pseudo, **kwargs):
        if not dex.exist_dask_object(X_train, y_train, X_eval, y_eval, X_pseudo, y_pseudo):
            return super().merge_pseudo_label(X_train, y_train, X_eval, y_eval, X_pseudo, y_pseudo, **kwargs)

        if self.resplit:
            x_list = [X_train, X_pseudo]
            y_list = [y_train, y_pseudo]
            if X_eval is not None and y_eval is not None:
                x_list.append(X_eval)
                y_list.append(y_eval)
            X_mix = dex.concat_df(x_list, axis=0)
            y_mix = dex.concat_df(y_list, axis=0)
            # if self.task == const.TASK_REGRESSION:
            #     stratify = None
            # else:
            #     stratify = y_mix

            X_mix = dex.concat_df([X_mix, y_mix], axis=1).reset_index(drop=True)
            y_mix = X_mix.pop(y_mix.name)

            eval_size = self.experiment.eval_size
            X_train, X_eval, y_train, y_eval = \
                dex.train_test_split(X_mix, y_mix, test_size=eval_size, random_state=self.random_state)
        else:
            X_train = dex.concat_df([X_train, X_pseudo], axis=0)
            y_train = dex.concat_df([y_train, y_pseudo], axis=0)

            # align divisions
            X_train = dex.concat_df([X_train, y_train], axis=1)
            y_train = X_train.pop(y_train.name)

        return X_train, y_train, X_eval, y_eval


class SteppedExperiment(Experiment):
    def __init__(self, steps, *args, **kwargs):
        assert isinstance(steps, (tuple, list)) and all([isinstance(step, ExperimentStep) for step in steps])
        super(SteppedExperiment, self).__init__(*args, **kwargs)

        if logger.is_info_enabled():
            names = [step.name for step in steps]
            logger.info(f'create experiment with {names}')
        self.steps = steps

    def train(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        for step in self.steps:
            if X_test is not None and X_train.columns.to_list() != X_test.columns.to_list():
                logger.warning(f'X_train{X_train.columns.to_list()} and X_test{X_test.columns.to_list()}'
                               f' have different columns before {step.name}, try fix it.')
                X_test = X_test[X_train.columns]
            if X_eval is not None and X_train.columns.to_list() != X_eval.columns.to_list():
                logger.warning(f'X_train{X_train.columns.to_list()} and X_eval{X_eval.columns.to_list()}'
                               f' have different columns before {step.name}, try fix it.')
                X_eval = X_eval[X_train.columns]

            X_train, y_train, X_test, X_eval, y_eval = \
                [v.persist() if dex.is_dask_object(v) else v for v in (X_train, y_train, X_test, X_eval, y_eval)]

            logger.info(f'fit_transform {step.name}')
            hyper_model, X_train, y_train, X_test, X_eval, y_eval = \
                step.fit_transform(hyper_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval, **kwargs)

        estimator = self.to_estimator(self.steps)
        self.hyper_model = hyper_model

        return estimator

    @staticmethod
    def to_estimator(steps):
        last_step = steps[-1]
        assert hasattr(last_step, 'estimator_')

        pipeline_steps = [(step.name, step) for step in steps if not step.is_transform_skipped()]

        if len(pipeline_steps) > 0:
            pipeline_steps += [('estimator', last_step.estimator_)]
            estimator = Pipeline(pipeline_steps)
            if logger.is_info_enabled():
                names = [step[0] for step in pipeline_steps]
                logger.info(f'trained experiment pipeline: {names}')
        else:
            estimator = last_step.estimator_
            if logger.is_info_enabled():
                logger.info(f'trained experiment estimator:\n{estimator}')

        return estimator


class CompeteExperiment(SteppedExperiment):
    """
    A powerful experiment strategy for AutoML with a set of advanced features.

    There are still many challenges in the machine learning modeling process for tabular data, such as imbalanced data,
    data drift, poor generalization ability, etc.  This challenges cannot be completely solved by pipeline search,
    so we introduced in HyperNets a more powerful tool is `CompeteExperiment`. `CompeteExperiment` is composed of a series
    of steps and *Pipeline Search* is just one step. It also includes advanced steps such as data cleaning,
    data drift handling, two-stage search, ensemble etc.
    """

    def __init__(self, hyper_model, X_train, y_train, X_eval=None, y_eval=None, X_test=None,
                 eval_size=DEFAULT_EVAL_SIZE,
                 train_test_split_strategy=None,
                 cv=True, num_folds=3,
                 task=None,
                 id=None,
                 callbacks=None,
                 random_state=9527,
                 scorer=None,
                 data_cleaner_args=None,
                 collinearity_detection=False,
                 drift_detection=True,
                 drift_detection_remove_shift_variable=True,
                 drift_detection_variable_shift_threshold=0.7,
                 drift_detection_threshold=0.7,
                 drift_detection_remove_size=0.1,
                 drift_detection_min_features=10,
                 drift_detection_num_folds=5,
                 feature_selection=False,
                 feature_selection_strategy=None,
                 feature_selection_threshold=None,
                 feature_selection_quantile=None,
                 feature_selection_number=None,
                 ensemble_size=20,
                 feature_reselection=False,
                 feature_reselection_estimator_size=10,
                 feature_reselection_threshold=1e-5,
                 pseudo_labeling=False,
                 pseudo_labeling_strategy=None,
                 pseudo_labeling_proba_threshold=None,
                 pseudo_labeling_proba_quantile=None,
                 pseudo_labeling_sample_number=None,
                 pseudo_labeling_resplit=False,
                 retrain_on_wholedata=False,
                 log_level=None,
                 **kwargs):
        """
        Parameters
        ----------
        hyper_model : hypernets.model.HyperModel
            A `HyperModel` instance
        X_train : Pandas or Dask DataFrame
            Feature data for training
        y_train : Pandas or Dask Series
            Target values for training
        X_eval : (Pandas or Dask DataFrame) or None
            (default=None), Feature data for evaluation
        y_eval : (Pandas or Dask Series) or None, (default=None)
            Target values for evaluation
        X_test : (Pandas or Dask Series) or None, (default=None)
            Unseen data without target values for semi-supervised learning
        eval_size : float or int, (default=None)
            Only valid when ``X_eval`` or ``y_eval`` is None. If float, should be between 0.0 and 1.0 and represent
            the proportion of the dataset to include in the eval split. If int, represents the absolute number of
            test samples. If None, the value is set to the complement of the train size.
        train_test_split_strategy : *'adversarial_validation'* or None, (default=None)
            Only valid when ``X_eval`` or ``y_eval`` is None. If None, use eval_size to split the dataset,
            otherwise use adversarial validation approach.
        cv : bool, (default=True)
            If True, use cross-validation instead of evaluation set reward to guide the search process
        num_folds : int, (default=3)
            Number of cross-validated folds, only valid when cv is true
        task : str or None, (default=None)
            Task type(*binary*, *multiclass* or *regression*).
            If None, inference the type of task automatically
        callbacks : list of callback functions or None, (default=None)
            List of callback functions that are applied at each experiment step. See `hypernets.experiment.ExperimentCallback`
            for more information.
        random_state : int or RandomState instance, (default=9527)
            Controls the shuffling applied to the data before applying the split
        scorer : str, callable or None, (default=None)
            Scorer to used for feature importance evaluation and ensemble. It can be a single string
            (see [get_scorer](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.get_scorer.html))
            or a callable (see [make_scorer](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.make_scorer.html)).
            If None, exception will occur.
        data_cleaner_args : dict, (default=None)
            dictionary of parameters to initialize the `DataCleaner` instance. If None, `DataCleaner` will initialized
            with default values.
        collinearity_detection :  bool, (default=False)
            Whether to clear multicollinearity features
        drift_detection : bool,(default=True)
            Whether to enable data drift detection and processing. Only valid when *X_test* is provided. Concept drift
            in the input data is one of the main challenges. Over time, it will worsen the performance of model on new
            data. We introduce an adversarial validation approach to concept drift problems. This approach will detect
            concept drift and identify the drifted features and process them automatically.
        drift_detection_remove_shift_variable : bool, (default=True)
        drift_detection_variable_shift_threshold : float, (default=0.7)
        drift_detection_threshold : float, (default=0.7)
        drift_detection_remove_size : float, (default=0.1)
        drift_detection_min_features : int, (default=10)
        drift_detection_num_folds : int, (default=5)
        feature_selection: bool, (default=False)
            Whether to select features by *feature_importances_*.
        feature_selection_strategy : str, (default='threshold')
            Strategy to select features(*threshold*, *number* or *quantile*).
        feature_selection_threshold : float, (default=0.1)
            Confidence threshold of feature_importance. Only valid when *feature_selection_strategy* is 'threshold'.
        feature_selection_quantile:
            Confidence quantile of feature_importance. Only valid when *feature_selection_strategy* is 'quantile'.
        feature_selection_number:
            Expected feature number to keep. Only valid when *feature_selection_strategy* is 'number'.
        feature_reselection : bool, (default=True)
            Whether to enable two stage feature selection and searching
        feature_reselection_estimator_size : int, (default=10)
            The number of estimator to evaluate feature importance. Only valid when *feature_reselection* is True.
        feature_reselection_threshold : float, (default=1e-5)
            The threshold for feature selection. Features with importance below the threshold will be dropped.  Only valid when *feature_reselection* is True.
        ensemble_size : int, (default=20)
            The number of estimator to ensemble. During the AutoML process, a lot of models will be generated with different
            preprocessing pipelines, different models, and different hyperparameters. Usually selecting some of the models
            that perform well to ensemble can obtain better generalization ability than just selecting the single best model.
        pseudo_labeling : bool, (default=False)
            Whether to enable pseudo labeling. Pseudo labeling is a semi-supervised learning technique, instead of manually
            labeling the unlabelled data, we give approximate labels on the basis of the labelled data. Pseudo-labeling can
            sometimes improve the generalization capabilities of the model.
        pseudo_labeling_strategy : str, (default='threshold')
            Strategy to sample pseudo labeling data(*threshold*, *number* or *quantile*).
        pseudo_labeling_proba_threshold : float, (default=0.8)
            Confidence threshold of pseudo-label samples. Only valid when *pseudo_labeling_strategy* is 'threshold'.
        pseudo_labeling_proba_quantile:
            Confidence quantile of pseudo-label samples. Only valid when *pseudo_labeling_strategy* is 'quantile'.
        pseudo_labeling_sample_number:
            Excepted number to sample per class. Only valid when *pseudo_labeling_strategy* is 'number'.
        pseudo_labeling_resplit : bool, (default=False)
            Whether to re-split the training set and evaluation set after adding pseudo-labeled data. If False, the
            pseudo-labeled data is only appended to the training set. Only valid when *pseudo_labeling* is True.
        retrain_on_wholedata : bool, (default=False)
            Whether to retrain the model with whole data after the search is completed.
        log_level : int, str, or None (default=None),
            Level of logging, possible values:
                -logging.CRITICAL
                -logging.FATAL
                -logging.ERROR
                -logging.WARNING
                -logging.WARN
                -logging.INFO
                -logging.DEBUG
                -logging.NOTSET
        kwargs :

        """
        steps = []
        two_stage = False
        enable_dask = dex.exist_dask_object(X_train, y_train, X_test, X_eval, y_eval)

        if enable_dask:
            search_cls, ensemble_cls, pseudo_cls = SpaceSearchStep, DaskEnsembleStep, DaskPseudoLabelStep
        else:
            search_cls, ensemble_cls, pseudo_cls = SpaceSearchStep, EnsembleStep, PseudoLabelStep

        # data clean
        steps.append(DataCleanStep(self, 'data_clean',
                                   data_cleaner_args=data_cleaner_args, cv=cv,
                                   train_test_split_strategy=train_test_split_strategy,
                                   random_state=random_state))

        # select by collinearity
        if collinearity_detection:
            steps.append(MulticollinearityDetectStep(self, 'collinearity_detection'))

        # drift detection
        if drift_detection:
            steps.append(DriftDetectStep(self, 'drift_detection',
                                         remove_shift_variable=drift_detection_remove_shift_variable,
                                         variable_shift_threshold=drift_detection_variable_shift_threshold,
                                         threshold=drift_detection_threshold,
                                         remove_size=drift_detection_remove_size,
                                         min_features=drift_detection_min_features,
                                         num_folds=drift_detection_num_folds))
        # feature selection by importance
        if feature_selection:
            steps.append(FeatureImportanceSelectionStep(
                self, 'feature_selection',
                strategy=feature_selection_strategy,
                threshold=feature_selection_threshold,
                quantile=feature_selection_quantile,
                number=feature_selection_number))

        # first-stage search
        steps.append(search_cls(self, 'space_search', cv=cv, num_folds=num_folds))

        # pseudo label
        if pseudo_labeling and task != const.TASK_REGRESSION:
            if ensemble_size is not None and ensemble_size > 1:
                estimator_builder = ensemble_cls(self, 'pseudo_ensemble', scorer=scorer, ensemble_size=ensemble_size)
            else:
                estimator_builder = FinalTrainStep(self, 'pseudo_train', retrain_on_wholedata=retrain_on_wholedata)
            step = pseudo_cls(self, 'pseudo_labeling',
                              estimator_builder=estimator_builder,
                              strategy=pseudo_labeling_strategy,
                              proba_threshold=pseudo_labeling_proba_threshold,
                              proba_quantile=pseudo_labeling_proba_quantile,
                              sample_number=pseudo_labeling_sample_number,
                              resplit=pseudo_labeling_resplit,
                              random_state=random_state)
            steps.append(step)
            two_stage = True

        # importance selection
        if feature_reselection:
            step = PermutationImportanceSelectionStep(self, 'feature_reselection',
                                                      scorer=scorer,
                                                      estimator_size=feature_reselection_estimator_size,
                                                      importance_threshold=feature_reselection_threshold)
            steps.append(step)
            two_stage = True

        # two-stage search
        if two_stage:
            steps.append(search_cls(self, 'two_stage_search', cv=cv, num_folds=num_folds))

        # final train
        if ensemble_size is not None and ensemble_size > 1:
            last_step = ensemble_cls(self, 'final_ensemble', scorer=scorer, ensemble_size=ensemble_size)
        else:
            last_step = FinalTrainStep(self, 'final_train', retrain_on_wholedata=retrain_on_wholedata)
        steps.append(last_step)

        # ignore warnings
        import warnings
        warnings.filterwarnings('ignore')

        if log_level is not None:
            _set_log_level(log_level)

        self.run_kwargs = kwargs
        super(CompeteExperiment, self).__init__(steps,
                                                hyper_model, X_train, y_train, X_eval=X_eval, y_eval=y_eval,
                                                X_test=X_test, eval_size=eval_size, task=task,
                                                id=id,
                                                callbacks=callbacks,
                                                random_state=random_state)

    def train(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        if _is_notebook:
            display_markdown('### Input Data', raw=True)

            if dex.exist_dask_object(X_train, y_train, X_test, X_eval, y_eval):
                display_data = (dex.compute(X_train.shape)[0],
                                dex.compute(y_train.shape)[0],
                                dex.compute(X_eval.shape)[0] if X_eval is not None else None,
                                dex.compute(y_eval.shape)[0] if y_eval is not None else None,
                                dex.compute(X_test.shape)[0] if X_test is not None else None,
                                self.task if self.task == const.TASK_REGRESSION
                                else f'{self.task}({dex.compute(y_train.nunique())[0]})')
            else:
                display_data = (X_train.shape,
                                y_train.shape,
                                X_eval.shape if X_eval is not None else None,
                                y_eval.shape if y_eval is not None else None,
                                X_test.shape if X_test is not None else None,
                                self.task if self.task == const.TASK_REGRESSION
                                else f'{self.task}({y_train.nunique()})')
            display(pd.DataFrame([display_data],
                                 columns=['X_train.shape',
                                          'y_train.shape',
                                          'X_eval.shape',
                                          'y_eval.shape',
                                          'X_test.shape',
                                          'Task', ]), display_id='output_intput')

            import seaborn as sns
            import matplotlib.pyplot as plt
            from sklearn.preprocessing import LabelEncoder

            le = LabelEncoder()
            y = le.fit_transform(y_train.dropna())
            # Draw Plot
            plt.figure(figsize=(8, 4), dpi=80)
            sns.distplot(y, kde=False, color="g", label="y")
            # Decoration
            plt.title('Distribution of y', fontsize=12)
            plt.legend()
            plt.show()

        return super().train(hyper_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)

    def run(self, **kwargs):
        run_kwargs = {**self.run_kwargs, **kwargs}
        return super().run(**run_kwargs)


def evaluate_oofs(hyper_model, ensemble_estimator, y_train, metrics):
    from hypernets.tabular.lifelong_learning import select_valid_oof
    from hypernets.tabular.metrics import calc_score
    trials = hyper_model.get_top_trials(ensemble_estimator.ensemble_size)
    if all(['oof' in trial.memo.keys() for trial in trials]):
        oofs = None
        for i, trial in enumerate(trials):
            if 'oof' in trial.memo.keys():
                oof = trial.memo['oof']
                if oofs is None:
                    if len(oof.shape) == 1:
                        oofs = np.zeros((oof.shape[0], len(trials)), dtype=np.float64)
                    else:
                        oofs = np.zeros((oof.shape[0], len(trials), oof.shape[-1]), dtype=np.float64)
                oofs[:, i] = oof
        y_, oofs_ = select_valid_oof(y_train, oofs)
        proba = ensemble_estimator.predictions2predict_proba(oofs_)
        pred = ensemble_estimator.predictions2predict(oofs_)
        scores = calc_score(y_, pred, proba, metrics)
        return scores
    else:
        print('No oof data')
        return None
