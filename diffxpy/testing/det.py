import abc
import logging
from typing import Union, Dict, Tuple, List, Set
import pandas as pd

import numpy as np
import xarray as xr
import patsy

from .utils import split_X, dmat_unique

try:
    import anndata
except ImportError:
    anndata = None

from batchglm.models.glm_nb import Model as GeneralizedLinearModel

from ..stats import stats
from . import correction
from diffxpy import pkg_constants

logger = logging.getLogger("diffxpy")


class _Estimation(GeneralizedLinearModel, metaclass=abc.ABCMeta):
    """
    Dummy class specifying all needed methods / parameters necessary for a model
    fitted for DifferentialExpressionTest.
    Useful for type hinting.
    """

    @property
    @abc.abstractmethod
    def X(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def design_loc(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def design_scale(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def constraints_loc(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def constraints_scale(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def num_observations(self) -> int:
        pass

    @property
    @abc.abstractmethod
    def num_features(self) -> int:
        pass

    @property
    @abc.abstractmethod
    def features(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def observations(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def log_likelihood(self, **kwargs) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def loss(self, **kwargs) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def gradients(self, **kwargs) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def hessians(self, **kwargs) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def fisher_inv(self, **kwargs) -> np.ndarray:
        pass


class _DifferentialExpressionTest(metaclass=abc.ABCMeta):
    """
    Dummy class specifying all needed methods / parameters necessary for DifferentialExpressionTest.
    Useful for type hinting. Structure:
    Methods which are called by constructor and which compute (corrected) p-values:
        _test()
        _correction()
    Accessor methods for important metrics which have to be extracted from estimated models:
        log_fold_change()
        reduced_model_gradient()
        full_model_gradient()
    Interface method which provides summary of results:
        results()
        plot()
    """

    def __init__(self):
        self._pval = None
        self._qval = None
        self._mean = None
        self._log_likelihood = None

    @property
    @abc.abstractmethod
    def gene_ids(self) -> np.ndarray:
        pass

    @property
    @abc.abstractmethod
    def X(self):
        pass

    @abc.abstractmethod
    def log_fold_change(self, base=np.e, **kwargs):
        pass

    def log2_fold_change(self, **kwargs):
        """
        Calculates the pairwise log_2 fold change(s) for this DifferentialExpressionTest.
        """
        return self.log_fold_change(base=2, **kwargs)

    def log10_fold_change(self, **kwargs):
        """
        Calculates the log_10 fold change(s) for this DifferentialExpressionTest.
        """
        return self.log_fold_change(base=10, **kwargs)

    def _test(self, **kwargs) -> np.ndarray:
        pass

    def _correction(self, method) -> np.ndarray:
        """
        Performs multiple testing corrections available in statsmodels.stats.multitest.multipletests()
        on self.pval.

        :param method: Multiple testing correction method.
            Browse available methods in the annotation of statsmodels.stats.multitest.multipletests().
        """
        if np.all(np.isnan(self.pval)):
            return self.pval
        else:
            return correction.correct(pvals=self.pval, method=method)

    def _ave(self):
        """
        Returns a xr.DataArray containing the mean expression by gene

        :return: xr.DataArray
        """
        pass

    @property
    def log_likelihood(self):
        if self._log_likelihood is None:
            self._log_likelihood = self._ll().compute()
        return self._log_likelihood

    @property
    def mean(self):
        if self._mean is None:
            self._mean = self._ave()
            if isinstance(self._mean, xr.DataArray):  # Could also be np.ndarray coming out of XArraySparseDataArray
                self._mean = self._mean.compute()
        return self._mean

    @property
    def pval(self):
        if self._pval is None:
            self._pval = self._test().copy()
        return self._pval

    @property
    def qval(self, method="fdr_bh"):
        if self._qval is None:
            self._qval = self._correction(method=method).copy()
        return self._qval

    def log10_pval_clean(self, log10_threshold=-30):
        """
        Return log10 transformed and cleaned p-values.

        NaN p-values are set to one and p-values below log10_threshold
        in log10 space are set to log10_threshold.

        :param log10_threshold: minimal log10 p-value to return.
        :return: Cleaned log10 transformed p-values.
        """
        pvals = np.reshape(self.pval, -1).astype(dtype=np.float)
        pvals = np.clip(
            pvals,
            np.nextafter(0, 1),
            np.inf
        )
        log10_pval_clean = np.log(pvals) / np.log(10)
        log10_pval_clean[np.isnan(log10_pval_clean)] = 1
        log10_pval_clean = np.clip(log10_pval_clean, log10_threshold, 0, log10_pval_clean)
        return log10_pval_clean

    def log10_qval_clean(self, log10_threshold=-30):
        """
        Return log10 transformed and cleaned q-values.

        NaN p-values are set to one and q-values below log10_threshold
        in log10 space are set to log10_threshold.

        :param log10_threshold: minimal log10 q-value to return.
        :return: Cleaned log10 transformed q-values.
        """
        qvals = np.reshape(self.qval, -1).astype(dtype=np.float)
        qvals = np.clip(
            qvals,
            np.nextafter(0, 1),
            np.inf
        )
        log10_qval_clean = np.log(qvals) / np.log(10)
        log10_qval_clean[np.isnan(log10_qval_clean)] = 1
        log10_qval_clean = np.clip(log10_qval_clean, log10_threshold, 0, log10_qval_clean)
        return log10_qval_clean

    @abc.abstractmethod
    def summary(self, **kwargs) -> pd.DataFrame:
        pass

    def _threshold_summary(
            self,
            res,
            qval_thres=None,
            fc_upper_thres=None,
            fc_lower_thres=None,
            mean_thres=None
    ) -> pd.DataFrame:
        """
        Reduce differential expression results into an output table with desired thresholds.
        """
        assert fc_lower_thres > 0 if fc_lower_thres is not None else True, "supply positive fc_lower_thres"
        assert fc_upper_thres > 0 if fc_upper_thres is not None else True, "supply positive fc_upper_thres"

        if qval_thres is not None:
            qvals = res['qval'].values
            qval_include = np.isnan(qvals) == False
            qval_include[qval_include] = qvals[qval_include] <= qval_thres
            res = res.iloc[qval_include, :]

        if fc_upper_thres is not None and fc_lower_thres is None:
            res = res.iloc[res['log2fc'].values >= np.log(fc_upper_thres) / np.log(2), :]
        elif fc_upper_thres is None and fc_lower_thres is not None:
            res = res.iloc[res['log2fc'].values <= np.log(fc_lower_thres) / np.log(2), :]
        elif fc_upper_thres is not None and fc_lower_thres is not None:
            res = res.iloc[np.logical_or(
                res['log2fc'].values <= np.log(fc_lower_thres) / np.log(2),
                res['log2fc'].values >= np.log(fc_upper_thres) / np.log(2)), :]

        if mean_thres is not None:
            res = res.iloc[res['mean'].values >= mean_thres, :]

        return res

    def plot_volcano(
            self,
            corrected_pval=True,
            log10_p_threshold=-30,
            log2_fc_threshold=10,
            alpha=0.05,
            min_fc=1,
            size=20,
            highlight_ids: List = [],
            highlight_size: float = 30,
            highlight_col: str = "red",
            show: bool = True,
            save: Union[str, None] = None,
            suffix: str = "_volcano.png"
    ):
        """
        Returns a volcano plot of p-value vs. log fold change

        :param corrected_pval: Whether to use multiple testing corrected
            or raw p-values.
        :param log10_p_threshold: lower bound of log10 p-values displayed in plot.
        :param log2_fc_threshold: Negative lower and upper bound of
            log2 fold change displayed in plot.
        :param alpha: p/q-value lower bound at which a test is considered
            non-significant. The corresponding points are colored in grey.
        :param min_fc: Fold-change lower bound for visualization,
            the points below the threshold are colored in grey.
        :param size: Size of points.
        :param highlight_ids: Genes to highlight in volcano plot.
        :param highlight_ids: Size of points of genes to highlight in volcano plot.
        :param highlight_ids: Color of points of genes to highlight in volcano plot.
        :param show: Whether (if save is not None) and where (save indicates dir and file stem) to display plot.
        :param save: Path+file name stem to save plots to.
            File will be save+suffix. Does not save if save is None.
        :param suffix: Suffix for file name to save plot to. Also use this to set the file type.

        :return: Tuple of matplotlib (figure, axis)
        """
        import seaborn as sns
        import matplotlib.pyplot as plt

        plt.ioff()

        if corrected_pval == True:
            neg_log_pvals = - self.log10_qval_clean(log10_threshold=log10_p_threshold)
        else:
            neg_log_pvals = - self.log10_pval_clean(log10_threshold=log10_p_threshold)

        logfc = np.reshape(self.log2_fold_change(), -1)
        # Clipping throws errors if not performed in actual data format (ndarray or DataArray):
        if isinstance(logfc, xr.DataArray):
            logfc = logfc.clip(-log2_fc_threshold, log2_fc_threshold)
        else:
            logfc = np.clip(logfc, -log2_fc_threshold, log2_fc_threshold, logfc)

        fig, ax = plt.subplots()

        is_significant = np.logical_and(
            neg_log_pvals >= - np.log(alpha) / np.log(10),
            np.abs(logfc) >= np.log(min_fc) / np.log(2)
        )

        sns.scatterplot(y=neg_log_pvals, x=logfc, hue=is_significant, ax=ax,
                        legend=False, s=size,
                        palette={True: "orange", False: "black"})

        highlight_ids_found = np.array([x in self.gene_ids for x in highlight_ids])
        highlight_ids_clean = [highlight_ids[i] for i in np.where(highlight_ids_found == True)[0]]
        highlight_ids_not_found = [highlight_ids[i] for i in np.where(highlight_ids_found == False)[0]]
        if len(highlight_ids_not_found) > 0:
            logger.warning("not all highlight_ids were found in data set: ", ", ".join(highlight_ids_not_found))

        if len(highlight_ids_clean) > 0:
            neg_log_pvals_highlights = np.zeros([len(highlight_ids_clean)])
            logfc_highlights = np.zeros([len(highlight_ids_clean)])
            is_highlight = np.zeros([len(highlight_ids_clean)])
            for i,id in enumerate(highlight_ids_clean):
                idx = np.where(self.gene_ids == id)[0]
                neg_log_pvals_highlights[i] = neg_log_pvals[idx]
                logfc_highlights[i] = logfc[idx]

            sns.scatterplot(y=neg_log_pvals_highlights, x=logfc_highlights,
                            hue=is_highlight, ax=ax,
                            legend=False, s=highlight_size,
                            palette={0: highlight_col})


        if corrected_pval == True:
            ax.set(xlabel="log2FC", ylabel='-log10(corrected p-value)')
        else:
            ax.set(xlabel="log2FC", ylabel='-log10(p-value)')

        # Save, show and return figure.
        if save is not None:
            plt.savefig(save + suffix)

        if show:
            plt.show()

        plt.close(fig)

        return ax

    def plot_ma(
            self,
            corrected_pval=True,
            log2_fc_threshold=10,
            min_mean=1e-4,
            alpha=0.05,
            size=20,
            highlight_ids: List = [],
            highlight_size: float = 30,
            highlight_col: str = "red",
            show: bool = True,
            save: Union[str, None] = None,
            suffix: str = "_my_plot.png"
    ):
        """
        Returns an MA plot of mean expression vs. log fold change with significance
        super-imposed.

        :param corrected_pval: Whether to use multiple testing corrected
            or raw p-values.
        :param log2_fc_threshold: Negative lower and upper bound of
            log2 fold change displayed in plot.
        :param min_mean:
            Lower bound for mean expression of plot. All values below this threshold
            are updated to this threshold.
        :param alpha: p/q-value lower bound at which a test is considered
            non-significant. The corresponding points are colored in grey.
        :param size: Size of points.
        :param highlight_ids: Genes to highlight in volcano plot.
        :param highlight_ids: Size of points of genes to highlight in volcano plot.
        :param highlight_ids: Color of points of genes to highlight in volcano plot.
        :param show: Whether (if save is not None) and where (save indicates dir and file stem) to display plot.
        :param save: Path+file name stem to save plots to.
            File will be save+suffix. Does not save if save is None.
        :param suffix: Suffix for file name to save plot to. Also use this to set the file type.


        :return: Tuple of matplotlib (figure, axis)
        """
        import seaborn as sns
        import matplotlib.pyplot as plt

        assert min_mean >= 0, "min_mean must be positive"

        plt.ioff()

        ave = np.log(np.clip(
            self.mean.astype(dtype=np.float),
            np.max(np.array([np.nextafter(0, 1), min_mean])),
            np.inf
        ))

        logfc = np.reshape(self.log2_fold_change(), -1)
        # Clipping throws errors if not performed in actual data format (ndarray or DataArray):
        if isinstance(logfc, xr.DataArray):
            logfc = logfc.clip(-log2_fc_threshold, log2_fc_threshold)
        else:
            logfc = np.clip(logfc, -log2_fc_threshold, log2_fc_threshold, logfc)

        fig, ax = plt.subplots()

        if corrected_pval:
            pvals = self.pval
            pvals[np.isnan(pvals)] = 1
            is_significant = pvals < alpha
        else:
            qvals = self.qval
            qvals[np.isnan(qvals)] = 1
            is_significant = qvals < alpha

        sns.scatterplot(y=logfc, x=ave, hue=is_significant, ax=ax,
                        legend=False, s=size,
                        palette={True: "orange", False: "black"})

        highlight_ids_found = np.array([x in self.gene_ids for x in highlight_ids])
        highlight_ids_clean = [highlight_ids[i] for i in np.where(highlight_ids_found == True)[0]]
        highlight_ids_not_found = [highlight_ids[i] for i in np.where(highlight_ids_found == False)[0]]
        if len(highlight_ids_not_found) > 0:
            logger.warning("not all highlight_ids were found in data set: ", ", ".join(highlight_ids_not_found))

        if len(highlight_ids_clean) > 0:
            ave_highlights = np.zeros([len(highlight_ids_clean)])
            logfc_highlights = np.zeros([len(highlight_ids_clean)])
            is_highlight = np.zeros([len(highlight_ids_clean)])
            for i,id in enumerate(highlight_ids_clean):
                idx = np.where(self.gene_ids == id)[0]
                ave_highlights[i] = ave[idx]
                logfc_highlights[i] = logfc[idx]

            sns.scatterplot(x=ave_highlights, y=logfc_highlights,
                            hue=is_highlight, ax=ax,
                            legend=False, s=highlight_size,
                            palette={0: highlight_col})

        ax.set(xlabel="log mean expression", ylabel="log2FC")

        # Save, show and return figure.
        if save is not None:
            plt.savefig(save + suffix)

        if show:
            plt.show()

        plt.close(fig)
        plt.ion()

        return ax


class _DifferentialExpressionTestSingle(_DifferentialExpressionTest, metaclass=abc.ABCMeta):
    """
    _DifferentialExpressionTest for unit_test with a single test per gene.
    The individual test object inherit directly from this class.

    All implementations of this class should return one p-value and one fold change per gene.
    """

    def summary(
            self,
            qval_thres=None,
            fc_upper_thres=None,
            fc_lower_thres=None,
            mean_thres=None,
            **kwargs
    ) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        assert self.gene_ids is not None

        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": self.pval,
            "qval": self.qval,
            "log2fc": self.log2_fold_change(),
            "mean": self.mean
        })

        return res


class DifferentialExpressionTestLRT(_DifferentialExpressionTestSingle):
    """
    Single log-likelihood ratio test per gene.
    """

    sample_description: pd.DataFrame
    full_design_loc_info: patsy.design_info
    full_estim: _Estimation
    reduced_design_loc_info: patsy.design_info
    reduced_estim: _Estimation

    def __init__(
            self,
            sample_description: pd.DataFrame,
            full_design_loc_info: patsy.design_info,
            full_estim,
            reduced_design_loc_info: patsy.design_info,
            reduced_estim
    ):
        super().__init__()
        self.sample_description = sample_description
        self.full_design_loc_info = full_design_loc_info
        self.full_estim = full_estim
        self.reduced_design_loc_info = reduced_design_loc_info
        self.reduced_estim = reduced_estim

    @property
    def gene_ids(self) -> np.ndarray:
        return np.asarray(self.full_estim.features)

    @property
    def X(self):
        return self.full_estim.X

    @property
    def reduced_model_gradient(self):
        return self.reduced_estim.gradients

    @property
    def full_model_gradient(self):
        return self.full_estim.gradients

    def _test(self):
        if np.any(self.full_estim.log_likelihood < self.reduced_estim.log_likelihood):
            logger.warning("Test assumption failed: full model is (partially) less probable than reduced model")

        return stats.likelihood_ratio_test(
            ll_full=self.full_estim.log_likelihood,
            ll_reduced=self.reduced_estim.log_likelihood,
            df_full=self.full_estim.constraints_loc.shape[1] + self.full_estim.constraints_scale.shape[1],
            df_reduced=self.reduced_estim.constraints_loc.shape[1] + self.reduced_estim.constraints_scale.shape[1],
        )

    def _ave(self):
        """
        Returns a xr.DataArray containing the mean expression by gene

        :return: xr.DataArray
        """

        return np.mean(self.full_estim.X, axis=0)

    def _log_fold_change(self, factors: Union[Dict, Tuple, Set, List], base=np.e):
        """
        Returns a xr.DataArray containing the locations for the different categories of the factors

        :param factors: the factors to select.
            E.g. `condition` or `batch` if formula would be `~ 1 + batch + condition`
        :param base: the log base to use; default is the natural logarithm
        :return: xr.DataArray
        """

        if not (isinstance(factors, list) or isinstance(factors, tuple) or isinstance(factors, set)):
            factors = {factors}
        if not isinstance(factors, set):
            factors = set(factors)

        di = self.full_design_loc_info
        sample_description = self.sample_description[[f.name() for f in di.subset(factors).factor_infos]]
        dmat = self.full_estim.design_loc

        # make rows unique
        dmat, sample_description = dmat_unique(dmat, sample_description)

        # factors = factors.intersection(di.term_names)

        # select the columns of the factors
        cols = np.arange(len(di.column_names))
        sel = np.concatenate([cols[di.slice(f)] for f in factors], axis=0)
        neg_sel = np.ones_like(cols).astype(bool)
        neg_sel[sel] = False

        # overwrite all columns which are not specified by the factors with 0
        dmat[:, neg_sel] = 0

        # make the design matrix + sample description unique again
        dmat, sample_description = dmat_unique(dmat, sample_description)

        locations = self.full_estim.inverse_link_loc(dmat.dot(self.full_estim.par_link_loc))
        locations = np.log(locations) / np.log(base)

        dist = np.expand_dims(locations, axis=0)
        dist = np.transpose(dist, [1, 0, 2]) - dist
        dist = xr.DataArray(dist, dims=("minuend", "subtrahend", "gene"))
        # retval = xr.Dataset({"logFC": retval})

        dist.coords["gene"] = self.gene_ids

        for col in sample_description:
            dist.coords["minuend_" + col] = (("minuend",), sample_description[col])
            dist.coords["subtrahend_" + col] = (("subtrahend",), sample_description[col])

        # # If this is a pairwise comparison, return only one fold change per gene
        # if dist.shape[:2] == (2, 2):
        #     dist = dist[1, 0]

        return dist

    def log_fold_change(self, base=np.e, return_type="vector"):
        """
        Calculates the pairwise log fold change(s) for this DifferentialExpressionTest.
        Returns some distance matrix representation of size (groups x groups x genes) where groups corresponds
        to the unique groups compared in this differential expression test.

        :param base: the log base to use; default is the natural logarithm
        :param return_type: Choose the return type.
            Possible values are:

            - "dataframe":
              return a pandas.DataFrame with columns `gene`, `minuend_<group>`, `subtrahend_<group>` and `logFC`.
            - "xarray":
              return a xarray.DataArray with dimensions `(minuend, subtrahend, gene)`

        :return: either pandas.DataFrame or xarray.DataArray
        """
        factors = set(self.full_design_loc_info.term_names) - set(self.reduced_design_loc_info.term_names)

        if return_type == "dataframe":
            dists = self._log_fold_change(factors=factors, base=base)

            df = dists.to_dataframe("logFC")
            df = df.reset_index().drop(["minuend", "subtrahend"], axis=1, errors="ignore")
            return df
        elif return_type == "vector":
            if len(factors) > 1 or self.sample_description[list(factors)].drop_duplicates().shape[0] != 2:
                return None
            else:
                dists = self._log_fold_change(factors=factors, base=base)
                return dists[1, 0].values
        else:
            dists = self._log_fold_change(factors=factors, base=base)
            return dists

    def locations(self):
        """
        Returns a pandas.DataFrame containing the locations for the different categories of the factors

        :return: pd.DataFrame
        """

        di = self.full_design_loc_info
        sample_description = self.sample_description[[f.name() for f in di.factor_infos]]
        dmat = self.full_estim.design_loc

        dmat, sample_description = dmat_unique(dmat, sample_description)

        retval = self.full_estim.inverse_link_loc(dmat.dot(self.full_estim.par_link_loc))
        retval = pd.DataFrame(retval, columns=self.full_estim.features)
        for col in sample_description:
            retval[col] = sample_description[col]

        retval = retval.set_index(list(sample_description.columns))

        return retval

    def scales(self):
        """
        Returns a pandas.DataFrame containing the scales for the different categories of the factors

        :return: pd.DataFrame
        """

        di = self.full_design_loc_info
        sample_description = self.sample_description[[f.name() for f in di.factor_infos]]
        dmat = self.full_estim.design_scale

        dmat, sample_description = dmat_unique(dmat, sample_description)

        retval = self.full_estim.inverse_link_scale(dmat.doc(self.full_estim.par_link_scale))
        retval = pd.DataFrame(retval, columns=self.full_estim.features)
        for col in sample_description:
            retval[col] = sample_description[col]

        retval = retval.set_index(list(sample_description.columns))

        return retval

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)
        res["grad"] = self.full_model_gradient.data
        res["grad_red"] = self.reduced_model_gradient.data

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class DifferentialExpressionTestWald(_DifferentialExpressionTestSingle):
    """
    Single wald test per gene.
    """

    model_estim: _Estimation
    coef_loc_totest: np.ndarray
    theta_mle: np.ndarray
    theta_sd: np.ndarray
    _error_codes: np.ndarray
    _niter: np.ndarray

    def __init__(
            self,
            model_estim: _Estimation,
            col_indices: np.ndarray
    ):
        """
        :param model_estim:
        :param cold_index: indices of coefs to test
        """
        super().__init__()

        self.model_estim = model_estim
        self.coef_loc_totest = col_indices

        try:
            if model_estim._error_codes is not None:
                self._error_codes = model_estim._error_codes
        except Exception as e:
            self._error_codes = None

        try:
            if model_estim._niter is not None:
                self._niter = model_estim._niter
        except Exception as e:
            self._niter = None

    @property
    def gene_ids(self) -> np.ndarray:
        return np.asarray(self.model_estim.features)

    @property
    def X(self):
        return self.model_estim.X

    @property
    def model_gradient(self):
        return self.model_estim.gradients

    def log_fold_change(self, base=np.e, **kwargs):
        """
        Returns one fold change per gene

        Returns coefficient if only one coefficient is testeed.
        Returns mean absolute coefficient if multiple coefficients are tested.
        """
        # design = np.unique(self.model_estim.design_loc, axis=0)
        # dmat = np.zeros_like(design)
        # dmat[:, self.coef_loc_totest] = design[:, self.coef_loc_totest]

        # loc = dmat @ self.model_estim.par_link_loc[self.coef_loc_totest]
        # return loc[1] - loc[0]
        if len(self.coef_loc_totest) == 1:
            return self.model_estim.a_var[self.coef_loc_totest][0]
        else:
            idx_max = np.argmax(np.abs(self.model_estim.a_var[self.coef_loc_totest]), axis=0)
            return self.model_estim.a_var[self.coef_loc_totest][
                idx_max, np.arange(self.model_estim.a_var.shape[1])]

    def _ll(self):
        """
        Returns a xr.DataArray containing the log likelihood of each gene

        :return: xr.DataArray
        """
        return self.model_estim.log_likelihood

    def _ave(self):
        """
        Returns a xr.DataArray containing the mean expression by gene

        :return: xr.DataArray
        """
        return self.X.mean(axis=0)

    def _test(self):
        """
        Returns a xr.DataArray containing the p-value for differential expression for each gene

        :return: xr.DataArray
        """
        # Check whether single- or multiple parameters are tested.
        # For a single parameter, the wald statistic distribution is approximated
        # with a normal distribution, for multiple parameters, a chi-square distribution is used.
        self.theta_mle = self.model_estim.a_var[self.coef_loc_totest]
        if len(self.coef_loc_totest) == 1:
            self.theta_mle = self.theta_mle[0]  # Make xarray one dimensional for stats.wald_test.
            self.theta_sd = self.model_estim.fisher_inv[:, self.coef_loc_totest[0], self.coef_loc_totest[0]].values
            self.theta_sd = np.nextafter(0, np.inf, out=self.theta_sd,
                                         where=self.theta_sd < np.nextafter(0, np.inf))
            self.theta_sd = np.sqrt(self.theta_sd)
            return stats.wald_test(
                theta_mle=self.theta_mle,
                theta_sd=self.theta_sd,
                theta0=0
            )
        else:
            self.theta_sd = np.diagonal(self.model_estim.fisher_inv, axis1=-2, axis2=-1).copy()
            self.theta_sd = np.nextafter(0, np.inf, out=self.theta_sd,
                                         where=self.theta_sd < np.nextafter(0, np.inf))
            self.theta_sd = np.sqrt(self.theta_sd)
            return stats.wald_test_chisq(
                theta_mle=self.theta_mle,
                theta_covar=self.model_estim.fisher_inv[:, self.coef_loc_totest, self.coef_loc_totest],
                theta0=0
            )

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)
        res["grad"] = self.model_gradient.data
        if len(self.theta_mle.shape) == 1:
            res["coef_mle"] = self.theta_mle
        if len(self.theta_sd.shape) == 1:
            res["coef_sd"] = self.theta_sd
        # add in info from bfgs
        if self.log_likelihood is not None:
            res["ll"] = self.log_likelihood
        if self._error_codes is not None:
            res["err"] = self._error_codes
        if self._niter is not None:
            res["niter"] = self._niter

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def plot_vs_ttest(self, log10=False):
        import matplotlib.pyplot as plt
        import seaborn as sns
        from .tests import t_test

        grouping = np.asarray(self.model_estim.design_loc[:, self.coef_loc_totest])
        ttest = t_test(
            data=self.model_estim.X,
            grouping=grouping,
            gene_names=self.gene_ids,
        )
        if log10:
            ttest_pvals = ttest.log10_pval_clean()
            pvals = self.log10_pval_clean()
        else:
            ttest_pvals = ttest.pval
            pvals = self.pval

        fig, ax = plt.subplots()

        sns.scatterplot(x=ttest_pvals, y=pvals, ax=ax)

        ax.set(xlabel="t-test", ylabel='wald test')

        return fig, ax


class DifferentialExpressionTestTT(_DifferentialExpressionTestSingle):
    """
    Single t-test test per gene.
    """

    def __init__(self, data, grouping, gene_names, is_logged):
        super().__init__()
        self._X = data
        self.grouping = grouping
        self._gene_names = np.asarray(gene_names)

        x0, x1 = split_X(data, grouping)

        # Only compute p-values for genes with non-zero observations and non-zero group-wise variance.
        mean_x0 = x0.mean(axis=0).astype(dtype=np.float)
        mean_x1 = x1.mean(axis=0).astype(dtype=np.float)
        # Avoid unnecessary mean computation:
        self._mean = np.average(
            a=np.vstack([mean_x0, mean_x1]),
            weights=np.array([x0.shape[0] / (x0.shape[0] + x1.shape[0]),
                              x1.shape[0] / (x0.shape[0] + x1.shape[0])]),
            axis=0,
            returned=False
        )
        self._ave_nonzero = self._mean != 0  # omit all-zero features
        var_x0 = np.asarray(x0.var(axis=0)).flatten().astype(dtype=np.float)
        var_x1 = np.asarray(x1.var(axis=0)).flatten().astype(dtype=np.float)
        self._var_geq_zero = np.logical_or(
            var_x0 > 0,
            var_x1 > 0
        )
        idx_run = np.where(np.logical_and(self._ave_nonzero, self._var_geq_zero))[0]
        pval = np.zeros([data.shape[1]]) + np.nan
        pval[idx_run] = stats.t_test_moments(
            mu0=mean_x0[idx_run],
            mu1=mean_x1[idx_run],
            var0=var_x0[idx_run],
            var1=var_x1[idx_run],
            n0=x0.shape[0],
            n1=x1.shape[0]
        )
        self._pval = pval

        if is_logged:
            self._logfc = mean_x1 - mean_x0
        else:
            self._logfc = np.log(mean_x1) - np.log(mean_x0)
        # Return 0 if LFC was non-zero and variances are zero,
        # this causes division by zero in the test statistic. This
        # is a highly significant result if one believes the variance estimate.
        # This is the default which can be changed and can be changed
        # via DIFFXPY_TREAT_ZEROVAR_TT_AS_SIG.
        pval[np.where(np.logical_and(np.logical_and(
            self._var_geq_zero == False,
            self._ave_nonzero == True),
            np.abs(self._logfc) < np.nextafter(0, 1)
        ))] = 0
        if pkg_constants.DE_TREAT_ZEROVAR_TT_AS_SIG:
            pval[np.where(np.logical_and(np.logical_and(
                self._var_geq_zero == False,
                self._ave_nonzero == True),
                np.abs(self._logfc) >= np.nextafter(0, 1)
            ))] = 1
        else:
            pval[np.where(np.logical_and(np.logical_and(
                self._var_geq_zero == False,
                self._ave_nonzero == True),
                np.abs(self._logfc) >= np.nextafter(0, 1)
            ))] = 0

    @property
    def gene_ids(self) -> np.ndarray:
        return self._gene_names

    @property
    def X(self):
        return self._X

    def log_fold_change(self, base=np.e, **kwargs):
        """
        Returns one fold change per gene
        """
        return self._logfc / np.log(base)

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)
        res["zero_mean"] = self._ave_nonzero == False
        res["zero_variance"] = self._var_geq_zero == False

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class DifferentialExpressionTestRank(_DifferentialExpressionTestSingle):
    """
    Single rank test per gene (Mann-Whitney U test).
    """

    def __init__(self, data, grouping, gene_names, is_logged):
        super().__init__()
        self._X = data
        self.grouping = grouping
        self._gene_names = np.asarray(gene_names)

        x0, x1 = split_X(data, grouping)

        mean_x0 = x0.mean(axis=0).astype(dtype=np.float)
        mean_x1 = x1.mean(axis=0).astype(dtype=np.float)
        # Avoid unnecessary mean computation:
        self._mean = np.average(
            a=np.vstack([mean_x0, mean_x1]),
            weights=np.array([x0.shape[0] / (x0.shape[0] + x1.shape[0]),
                              x1.shape[0] / (x0.shape[0] + x1.shape[0])]),
            axis=0,
            returned=False
        )
        var_x0 = np.asarray(x0.var(axis=0)).flatten().astype(dtype=np.float)
        var_x1 = np.asarray(x1.var(axis=0)).flatten().astype(dtype=np.float)
        self._var_geq_zero = np.logical_or(
            var_x0 > 0,
            var_x1 > 0
        )
        idx_run = np.where(np.logical_and(self._mean != 0, self._var_geq_zero))[0]

        # TODO: can this be done on sparse?
        pval = np.zeros([data.shape[1]]) + np.nan
        if isinstance(x0, xr.DataArray):
            pval[idx_run] = stats.mann_whitney_u_test(
                x0=x0.data[:, idx_run],
                x1=x1.data[:, idx_run]
            )
        else:
            pval[idx_run] = stats.mann_whitney_u_test(
                x0=x0.X[:, idx_run].toarray(),
                x1=x1.X[:, idx_run].toarray()
            )

        self._pval = pval

        if is_logged:
            self._logfc = mean_x1 - mean_x0
        else:
            self._logfc = np.log(mean_x1) - np.log(mean_x0)

    @property
    def gene_ids(self) -> np.ndarray:
        return self._gene_names

    @property
    def X(self):
        return self._X

    def log_fold_change(self, base=np.e, **kwargs):
        """
        Returns one fold change per gene
        """
        if base == np.e:
            return self._logfc
        else:
            return self._logfc / np.log(base)

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def plot_vs_ttest(self, log10=False):
        import matplotlib.pyplot as plt
        import seaborn as sns
        from .tests import t_test

        grouping = self.grouping
        ttest = t_test(
            data=self.X,
            grouping=grouping,
            gene_names=self.gene_ids,
        )
        if log10:
            ttest_pvals = ttest.log10_pval_clean()
            pvals = self.log10_pval_clean()
        else:
            ttest_pvals = ttest.pval
            pvals = self.pval

        fig, ax = plt.subplots()

        sns.scatterplot(x=ttest_pvals, y=pvals, ax=ax)

        ax.set(xlabel="t-test", ylabel='rank test')

        return fig, ax


class _DifferentialExpressionTestMulti(_DifferentialExpressionTest, metaclass=abc.ABCMeta):
    """
    _DifferentialExpressionTest for unit_test with a multiple unit_test per gene.
    The individual test object inherit directly from this class.
    """

    def __init__(self, correction_type: str):
        """

        :param correction_type: Choose between global and test-wise correction.
            Can be:

            - "global": correct all p-values in one operation
            - "by_test": correct the p-values of each test individually
        """
        super().__init__()
        self._correction_type = correction_type

    def _correction(self, method):
        if self._correction_type.lower() == "global":
            pvals = np.reshape(self.pval, -1)
            qvals = correction.correct(pvals=pvals, method=method)
            qvals = np.reshape(qvals, self.pval.shape)
            return qvals
        elif self._correction_type.lower() == "by_test":
            qvals = np.apply_along_axis(
                func1d=lambda pvals: correction.correct(pvals=pvals, method=method),
                axis=-1,
                arr=self.pval,
            )
            return qvals

    def summary(self, **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.

        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the minimum per-gene p-value of all tests
            - qval: the minimum per-gene q-value of all tests
            - log2fc: the maximal/minimal (depending on which one is higher) log2 fold change of the genes
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None

        # calculate maximum logFC of lower triangular fold change matrix
        raw_logfc = self.log2_fold_change()

        # first flatten all dimensions up to the last 'gene' dimension
        flat_logfc = raw_logfc.reshape(-1, raw_logfc.shape[-1])
        # next, get argmax of flattened logfc and unravel the true indices from it
        r, c = np.unravel_index(flat_logfc.argmax(0), raw_logfc.shape[:2])
        # if logfc is maximal in the lower triangular matrix, multiply it with -1
        logfc = raw_logfc[r, c, np.arange(raw_logfc.shape[-1])] * np.where(r <= c, 1, -1)

        res = pd.DataFrame({
            "gene": self.gene_ids,
            # return minimal pval by gene:
            "pval": np.min(self.pval.reshape(-1, self.pval.shape[-1]), axis=0),
            # return minimal qval by gene:
            "qval": np.min(self.qval.reshape(-1, self.qval.shape[-1]), axis=0),
            # return maximal logFC by gene:
            "log2fc": np.asarray(logfc),
            # return mean expression across all groups by gene:
            "mean": np.asarray(self.mean)
        })

        return res


class DifferentialExpressionTestPairwise(_DifferentialExpressionTestMulti):
    """
    Pairwise unit_test between more than 2 groups per gene.
    """

    def __init__(self, gene_ids, pval, logfc, ave, groups, tests, correction_type: str):
        super().__init__(correction_type=correction_type)
        self._gene_ids = np.asarray(gene_ids)
        self._logfc = logfc
        self._pval = pval
        self._mean = ave
        self.groups = list(np.asarray(groups))
        self._tests = tests

        q = self.qval

    @property
    def gene_ids(self) -> np.ndarray:
        return self._gene_ids

    @property
    def X(self):
        return None

    @property
    def tests(self):
        """
        If `keep_full_test_objs` was set to `True`, this will return a matrix of differential expression tests.
        """
        if self._tests is None:
            raise ValueError("Individual tests were not kept!")

        return self._tests

    def log_fold_change(self, base=np.e, **kwargs):
        """
        Returns matrix of fold changes per gene
        """
        if base == np.e:
            return self._logfc
        else:
            return self._logfc / np.log(base)

    def _check_groups(self, group1, group2):
        if group1 not in self.groups:
            raise ValueError('group1 not recognized')
        if group2 not in self.groups:
            raise ValueError('group2 not recognized')

    def pval_pair(self, group1, group2):
        """
        Get p-values of the comparison of group1 and group2.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :return: p-values
        """
        assert self._pval is not None

        self._check_groups(group1, group2)
        return self._pval[self.groups.index(group1), self.groups.index(group2), :]

    def qval_pair(self, group1, group2):
        """
        Get q-values of the comparison of group1 and group2.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :return: q-values
        """
        assert self._qval is not None

        self._check_groups(group1, group2)
        return self._qval[self.groups.index(group1), self.groups.index(group2), :]

    def log10_pval_pair_clean(self, group1, group2, log10_threshold=-30):
        """
        Return log10 transformed and cleaned p-values.

        NaN p-values are set to one and p-values below log10_threshold
        in log10 space are set to log10_threshold.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :param log10_threshold: minimal log10 p-value to return.
        :return: Cleaned log10 transformed p-values.
        """
        pvals = np.reshape(self.pval_pair(group1=group1, group2=group2), -1)
        pvals = np.nextafter(0, 1, out=pvals, where=pvals == 0)
        log10_pval_clean = np.log(pvals) / np.log(10)
        log10_pval_clean[np.isnan(log10_pval_clean)] = 1
        log10_pval_clean = np.clip(log10_pval_clean, log10_threshold, 0, log10_pval_clean)
        return log10_pval_clean

    def log10_qval_pair_clean(self, group1, group2, log10_threshold=-30):
        """
        Return log10 transformed and cleaned q-values.

        NaN p-values are set to one and q-values below log10_threshold
        in log10 space are set to log10_threshold.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :param log10_threshold: minimal log10 q-value to return.
        :return: Cleaned log10 transformed q-values.
        """
        qvals = np.reshape(self.qval_pair(group1=group1, group2=group2), -1)
        qvals = np.nextafter(0, 1, out=qvals, where=qvals == 0)
        log10_qval_clean = np.log(qvals) / np.log(10)
        log10_qval_clean[np.isnan(log10_qval_clean)] = 1
        log10_qval_clean = np.clip(log10_qval_clean, log10_threshold, 0, log10_qval_clean)
        return log10_qval_clean

    def log_fold_change_pair(self, group1, group2, base=np.e):
        """
        Get log fold changes of the comparison of group1 and group2.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :return: log fold changes
        """
        assert self._logfc is not None

        self._check_groups(group1, group2)
        return self.log_fold_change(base=base)[self.groups.index(group1), self.groups.index(group2), :]

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def summary_pair(self, group1, group2,
                     qval_thres=None, fc_upper_thres=None,
                     fc_lower_thres=None, mean_thres=None,
                     **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.

        :param group1: Identifier of first group of observations in pair-wise comparison.
        :param group2: Identifier of second group of observations in pair-wise comparison.
        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the per-gene p-value of the selected test
            - qval: the per-gene q-value of the selected test
            - log2fc: the per-gene log2 fold change of the selected test
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None

        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": self.pval_pair(group1=group1, group2=group2),
            "qval": self.qval_pair(group1=group1, group2=group2),
            "log2fc": self.log_fold_change_pair(group1=group1, group2=group2, base=2),
            "mean": np.asarray(self.mean)
        })

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class DifferentialExpressionTestZTest(_DifferentialExpressionTestMulti):
    """
    Pairwise unit_test between more than 2 groups per gene.
    """

    model_estim: _Estimation
    theta_mle: np.ndarray
    theta_sd: np.ndarray

    def __init__(self, model_estim: _Estimation, grouping, groups, correction_type: str):
        super().__init__(correction_type=correction_type)
        self.model_estim = model_estim
        self.grouping = grouping
        self.groups = list(np.asarray(groups))

        # values of parameter estimates: coefficients x genes array with one coefficient per group
        self._theta_mle = model_estim.par_link_loc
        # standard deviation of estimates: coefficients x genes array with one coefficient per group
        # theta_sd = sqrt(diagonal(fisher_inv))
        self._theta_sd = np.sqrt(np.diagonal(model_estim.fisher_inv, axis1=-2, axis2=-1)).T
        self._logfc = None

        # Call tests in constructor.
        p = self.pval
        q = self.qval

    def _test(self, **kwargs):
        groups = self.groups
        num_features = self.model_estim.X.shape[1]

        pvals = np.tile(np.NaN, [len(groups), len(groups), num_features])
        pvals[np.eye(pvals.shape[0]).astype(bool)] = 1

        theta_mle = self._theta_mle
        theta_sd = self._theta_sd

        for i, g1 in enumerate(groups):
            for j, g2 in enumerate(groups[(i + 1):]):
                j = j + i + 1

                pvals[i, j] = stats.two_coef_z_test(theta_mle0=theta_mle[i], theta_mle1=theta_mle[j],
                                                    theta_sd0=theta_sd[i], theta_sd1=theta_sd[j])
                pvals[j, i] = pvals[i, j]

        return pvals

    @property
    def gene_ids(self) -> np.ndarray:
        return np.asarray(self.model_estim.features)

    @property
    def X(self):
        return self.model_estim.X

    @property
    def log_likelihood(self):
        return np.sum(self.model_estim.log_probs(), axis=0)

    @property
    def model_gradient(self):
        return self.model_estim.gradients

    def _ave(self):
        """
        Returns a xr.DataArray containing the mean expression by gene

        :return: xr.DataArray
        """

        return np.mean(self.model_estim.X, axis=0)

    def log_fold_change(self, base=np.e, **kwargs):
        """
        Returns matrix of fold changes per gene
        """
        if self._logfc is None:
            groups = self.groups
            num_features = self.model_estim.X.shape[1]

            logfc = np.tile(np.NaN, [len(groups), len(groups), num_features])
            logfc[np.eye(logfc.shape[0]).astype(bool)] = 0

            theta_mle = self._theta_mle

            for i, g1 in enumerate(groups):
                for j, g2 in enumerate(groups[(i + 1):]):
                    j = j + i + 1

                    logfc[i, j] = theta_mle[j] - theta_mle[i]
                    logfc[j, i] = -logfc[i, j]

            self._logfc = logfc

        if base == np.e:
            return self._logfc
        else:
            return self._logfc / np.log(base)

    def _check_groups(self, group1, group2):
        if group1 not in self.groups:
            raise ValueError('group1 not recognized')
        if group2 not in self.groups:
            raise ValueError('group2 not recognized')

    def pval_pair(self, group1, group2):
        self._check_groups(group1, group2)
        return self.pval[self.groups.index(group1), self.groups.index(group2), :]

    def qval_pair(self, group1, group2):
        self._check_groups(group1, group2)
        return self.qval[self.groups.index(group1), self.groups.index(group2), :]

    def log_fold_change_pair(self, group1, group2, base=np.e):
        self._check_groups(group1, group2)
        return self.log_fold_change(base=base)[self.groups.index(group1), self.groups.index(group2), :]

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def summary_pair(self, group1, group2,
                     qval_thres=None, fc_upper_thres=None,
                     fc_lower_thres=None, mean_thres=None,
                     **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.

        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the per-gene p-value of the selected test
            - qval: the per-gene q-value of the selected test
            - log2fc: the per-gene log2 fold change of the selected test
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None

        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": self.pval_pair(group1=group1, group2=group2),
            "qval": self.qval_pair(group1=group1, group2=group2),
            "log2fc": self.log_fold_change_pair(group1=group1, group2=group2, base=2),
            "mean": np.asarray(self.mean)
        })

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

class DifferentialExpressionTestZTestLazy(_DifferentialExpressionTestMulti):
    """
    Pairwise unit_test between more than 2 groups per gene with lazy evaluation.

    This class performs pairwise tests upon enquiry only and does not store them
    and is therefore suited so very large group sets for which the lfc and
    p-value matrices of the size [genes, groups, groups] are too big to fit into
    memory.
    """

    model_estim: _Estimation
    _theta_mle: np.ndarray
    _theta_sd: np.ndarray

    def __init__(self, model_estim: _Estimation, grouping, groups, correction_type="global"):
        super().__init__(correction_type=correction_type)
        self.model_estim = model_estim
        self.grouping = grouping
        if isinstance(groups, list):
            self.groups = groups
        else:
            self.groups = groups.tolist()

        # values of parameter estimates: coefficients x genes array with one coefficient per group
        self._theta_mle = model_estim.par_link_loc
        # standard deviation of estimates: coefficients x genes array with one coefficient per group
        # theta_sd = sqrt(diagonal(fisher_inv))
        self._theta_sd = np.sqrt(np.diagonal(model_estim.fisher_inv, axis1=-2, axis2=-1)).T

    def _correction(self, pvals, method="fdr_bh") -> np.ndarray:
        """
        Performs multiple testing corrections available in statsmodels.stats.multitest.multipletests().

        This overwrites the parent function which uses self.pval which is not used in this
        lazy implementation.

        :param pvals: P-value array to correct.
        :param method: Multiple testing correction method.
            Browse available methods in the annotation of statsmodels.stats.multitest.multipletests().
        """
        if self._correction_type.lower() == "global":
            pval_shape = pvals.shape
            pvals = np.reshape(pvals, -1)
            qvals = correction.correct(pvals=pvals, method=method)
            qvals = np.reshape(qvals, pval_shape)
        elif self._correction_type.lower() == "by_test":
            qvals = np.apply_along_axis(
                func1d=lambda p: correction.correct(pvals=p, method=method),
                axis=-1,
                arr=pvals,
            )
        else:
            raise ValueError("method " + method + " not recognized in _correction()")

        return qvals

    def _test(self, **kwargs):
        """
        This function is not available in lazy results evaluation as it would
        require all pairwise tests to be performed.
        """
        pass

    def _test_pairs(self, groups0, groups1, **kwargs):
        num_features = self.model_estim.X.shape[1]

        pvals = np.tile(np.NaN, [len(groups0), len(groups1), num_features])

        for i,g0 in enumerate(groups0):
            for j,g1 in enumerate(groups1):
                if g0 != g1:
                    pvals[i, j] = stats.two_coef_z_test(
                        theta_mle0=self._theta_mle[g0],
                        theta_mle1=self._theta_mle[g1],
                        theta_sd0=self._theta_sd[g0],
                        theta_sd1=self._theta_sd[g1]
                    )
                else:
                    pvals[i, j] = 1

        return pvals

    @property
    def gene_ids(self) -> np.ndarray:
        return np.asarray(self.model_estim.features)

    @property
    def X(self):
        return self.model_estim.X

    @property
    def log_likelihood(self):
        return np.sum(self.model_estim.log_probs(), axis=0)

    @property
    def model_gradient(self):
        return self.model_estim.gradients

    def _ave(self):
        """
        Returns a xr.DataArray containing the mean expression by gene

        :return: xr.DataArray
        """

        return np.mean(self.model_estim.X, axis=0)

    @property
    def pval(self, **kwargs):
        """
        This function is not available in lazy results evaluation as it would
        require all pairwise tests to be performed.
        """
        pass

    @property
    def qval(self, **kwargs):
        """
        This function is not available in lazy results evaluation as it would
        require all pairwise tests to be performed.
        """
        pass

    def log_fold_change(self, base=np.e, **kwargs):
        """
        This function is not available in lazy results evaluation as it would
        require all pairwise tests to be performed.
        """
        pass

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        This function is not available in lazy results evaluation as it would
        require all pairwise tests to be performed.
        """
        pass

    def _check_groups(self, groups0, groups1):
        if isinstance(groups0, list)==False:
            groups0 = [groups0]
        if isinstance(groups1, list)==False:
            groups1 = [groups1]
        for g in groups0:
            if g not in self.groups:
                raise ValueError('groups0 element '+str(g)+' not recognized')
        for g in groups1:
            if g not in self.groups:
                raise ValueError('groups1 element '+str(g)+' not recognized')

    def _groups_idx(self, groups):
        if isinstance(groups, list)==False:
            groups = [groups]
        return np.array([self.groups.index(x) for x in groups])

    def pval_pairs(self, groups0=None, groups1=None):
        """
        Return p-values for all pairwise comparisons of groups0 and groups1.

        If you want to test one group (such as a control) against all other groups
        (one test for each other group), give the control group id in groups0
        and leave groups1=None, groups1 is then set to the full set of all groups.

        :param groups0: First set of groups in pair-wise comparison.
        :param groups1: Second set of groups in pair-wise comparison.
        :return: P-values of pair-wise comparison.
        """
        if groups0 is None:
            groups0 = self.groups
        if groups1 is None:
            groups1 = self.groups
        self._check_groups(groups0, groups1)
        groups0 = self._groups_idx(groups0)
        groups1 = self._groups_idx(groups1)
        return self._test_pairs(groups0=groups0, groups1=groups1)

    def qval_pairs(self, groups0=None, groups1=None, method="fdr_bh", **kwargs):
        """
        Return multiple testing-corrected p-values for all
        pairwise comparisons of groups0 and groups1.

        If you want to test one group (such as a control) against all other groups
        (one test for each other group), give the control group id in groups0
        and leave groups1=None, groups1 is then set to the full set of all groups.

        :param groups0: First set of groups in pair-wise comparison.
        :param groups1: Second set of groups in pair-wise comparison.
        :param method: Multiple testing correction method.
            Browse available methods in the annotation of statsmodels.stats.multitest.multipletests().
        :return: Multiple testing-corrected p-values of pair-wise comparison.
        """
        if groups0 is None:
            groups0 = self.groups
        if groups1 is None:
            groups1 = self.groups
        self._check_groups(groups0, groups1)
        groups0 = self._groups_idx(groups0)
        groups1 = self._groups_idx(groups1)
        pval = self.pval_pair(groups0=groups0, groups1=groups1)
        return self._correction(pval=pval, method=method, **kwargs)

    def log_fold_change_pairs(self, groups0=None, groups1=None, base=np.e):
        """
        Return log-fold changes for all pairwise comparisons of groups0 and groups1.

        If you want to test one group (such as a control) against all other groups
        (one test for each other group), give the control group id in groups0
        and leave groups1=None, groups1 is then set to the full set of all groups.

        :param groups0: First set of groups in pair-wise comparison.
        :param groups1: Second set of groups in pair-wise comparison.
        :param base: Base of logarithm of log-fold change.
        :return: P-values of pair-wise comparison.
        """
        if groups0 is None:
            groups0 = self.groups
        if groups1 is None:
            groups1 = self.groups
        self._check_groups(groups0, groups1)
        groups0 = self._groups_idx(groups0)
        groups1 = self._groups_idx(groups1)

        num_features = self._theta_mle.shape[1]

        logfc = np.zeros(shape=(len(groups0), len(groups1), num_features))
        for i,g0 in enumerate(groups0):
            for j,g1 in enumerate(groups1):
                logfc[i,j,:] = self._theta_mle[g0,:].values - self._theta_mle[g1,:].values

        if base == np.e:
            return logfc
        else:
            return logfc / np.log(base)

    def summary_pair(self, group0, group1,
                     qval_thres=None, fc_upper_thres=None,
                     fc_lower_thres=None, mean_thres=None,
                     **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results of single pairwose comparison
        into an output table.

        :param group0: Firt group in pair-wise comparison.
        :param group1: Second group in pair-wise comparison.
        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the per-gene p-value of the selected test
            - qval: the per-gene q-value of the selected test
            - log2fc: the per-gene log2 fold change of the selected test
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None

        if len(group0) != 1:
            raise ValueError("group0 should only contain one entry in summary_pair()")
        if len(group1) != 1:
            raise ValueError("group1 should only contain one entry in summary_pair()")

        pval = self.pval_pairs(groups0=group0, groups1=group1)
        qval = self._correction(pvals=pval, **kwargs)
        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": pval.flatten(),
            "qval": qval.flatten(),
            "log2fc": self.log_fold_change_pairs(groups0=group0, groups1=group1, base=2).flatten(),
            "mean": np.asarray(self.mean)
        })

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def summary_pairs(self, groups0, groups1=None,
                     qval_thres=None, fc_upper_thres=None,
                     fc_lower_thres=None, mean_thres=None,
                     **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results of a set of
        pairwise comparisons into an output table.

        :param groups0: First set of groups in pair-wise comparison.
        :param groups1: Second set of groups in pair-wise comparison.
        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the minimum per-gene p-value of all tests
            - qval: the minimum per-gene q-value of all tests
            - log2fc: the maximal/minimal (depending on which one is higher) log2 fold change of the genes
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None
        if groups1 is None:
            groups1 = self.groups

        pval = self.pval_pairs(groups0=groups0, groups1=groups1)
        qval = self._correction(pvals=pval, **kwargs)

        # calculate maximum logFC of lower triangular fold change matrix
        raw_logfc = self.log_fold_change_pairs(groups0=groups0, groups1=groups1, base=2)

        # first flatten all dimensions up to the last 'gene' dimension
        flat_logfc = raw_logfc.reshape(-1, raw_logfc.shape[-1])
        # next, get argmax of flattened logfc and unravel the true indices from it
        r, c = np.unravel_index(flat_logfc.argmax(0), raw_logfc.shape[:2])
        # if logfc is maximal in the lower triangular matrix, multiply it with -1
        logfc = raw_logfc[r, c, np.arange(raw_logfc.shape[-1])] * np.where(r <= c, 1, -1)

        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": np.min(pval, axis=(0,1)),
            "qval": np.min(qval, axis=(0,1)),
            "log2fc": np.asarray(logfc),
            "mean": np.asarray(self.mean)
        })

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class DifferentialExpressionTestVsRest(_DifferentialExpressionTestMulti):
    """
    Tests between between each group and the rest for more than 2 groups per gene.
    """

    def __init__(self, gene_ids, pval, logfc, ave, groups, tests, correction_type: str):
        super().__init__(correction_type=correction_type)
        self._gene_ids = np.asarray(gene_ids)
        self._pval = pval
        self._logfc = logfc
        self._mean = ave
        self.groups = list(np.asarray(groups))
        self._tests = tests

        q = self.qval

    @property
    def tests(self):
        """
        If `keep_full_test_objs` was set to `True`, this will return a matrix of differential expression tests.
        """
        if self._tests is None:
            raise ValueError("Individual tests were not kept!")

        return self._tests

    @property
    def gene_ids(self) -> np.ndarray:
        return self._gene_ids

    @property
    def X(self) -> np.ndarray:
        return None

    def log_fold_change(self, base=np.e, **kwargs):
        if base == np.e:
            return self._logfc
        else:
            return self._logfc / np.log(base)

    def _check_group(self, group):
        if group not in self.groups:
            raise ValueError('group "%s" not recognized' % group)

    def pval_group(self, group):
        self._check_group(group)
        return self.pval[0, self.groups.index(group), :]

    def qval_group(self, group):
        self._check_group(group)
        return self.qval[0, self.groups.index(group), :]

    def log_fold_change_group(self, group, base=np.e):
        self._check_group(group)
        return self.log_fold_change(base=base)[0, self.groups.index(group), :]

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def summary_group(self, group,
                      qval_thres=None, fc_upper_thres=None,
                      fc_lower_thres=None, mean_thres=None,
                      **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.

        :return: pandas.DataFrame with the following columns:

            - gene: the gene id's
            - pval: the per-gene p-value of the selected test
            - qval: the per-gene q-value of the selected test
            - log2fc: the per-gene log2 fold change of the selected test
            - mean: the mean expression of the gene across all groups
        """
        assert self.gene_ids is not None

        res = pd.DataFrame({
            "gene": self.gene_ids,
            "pval": self.pval_group(group=group),
            "qval": self.qval_group(group=group),
            "log2fc": self.log_fold_change_group(group=group, base=2),
            "mean": np.asarray(self.mean)
        })

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class DifferentialExpressionTestByPartition(_DifferentialExpressionTestMulti):
    """
    Stores a particular test performed within each partition of the data set.
    """

    def __init__(self, partitions, tests, ave, correction_type: str = "by_test"):
        super().__init__(correction_type=correction_type)
        self.partitions = list(np.asarray(partitions))
        self._tests = tests
        self._gene_ids = tests[0].gene_ids
        self._pval = np.expand_dims(np.vstack([x.pval for x in tests]), axis=0)
        self._logfc = np.expand_dims(np.vstack([x.log_fold_change() for x in tests]), axis=0)
        self._mean = ave

        q = self.qval

    @property
    def gene_ids(self) -> np.ndarray:
        return self._gene_ids

    @property
    def X(self) -> np.ndarray:
        return None

    def log_fold_change(self, base=np.e, **kwargs):
        if base == np.e:
            return self._logfc
        else:
            return self._logfc / np.log(base)

    def _check_partition(self, partition):
        if partition not in self.partitions:
            raise ValueError('partition "%s" not recognized' % partition)

    @property
    def tests(self, partition=None):
        """
        If `keep_full_test_objs` was set to `True`, this will return a matrix of differential expression tests.

        :param partition: The partition for which to return the test. Returns full list if None.
        """
        if self._tests is None:
            raise ValueError("Individual tests were not kept!")

        if partition is None:
            return self._tests
        else:
            self._check_partition(partition)
            return self._tests[self.partitions.index(partition)]

    def summary(self, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None,
                **kwargs) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.
        """
        res = super().summary(**kwargs)

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res


class _DifferentialExpressionTestCont(_DifferentialExpressionTestSingle):
    _de_test: _DifferentialExpressionTestSingle
    _model_estim: _Estimation
    _size_factors: np.ndarray
    _continuous_coords: np.ndarray
    _spline_coefs: list

    def __init__(
            self,
            de_test: _DifferentialExpressionTestSingle,
            model_estim: _Estimation,
            size_factors: np.ndarray,
            continuous_coords: str,
            spline_coefs: list
    ):
        self._de_test = de_test
        self._model_estim = model_estim
        self._size_factors = size_factors
        self._continuous_coords = continuous_coords
        self._spline_coefs = spline_coefs

    @property
    def gene_ids(self) -> np.ndarray:
        return self._de_test.gene_ids

    @property
    def X(self):
        return self._de_test.X

    @property
    def pval(self) -> np.ndarray:
        return self._de_test.pval

    @property
    def qval(self) -> np.ndarray:
        return self._de_test.qval

    @property
    def mean(self) -> np.ndarray:
        return self._de_test.mean

    @property
    def log_likelihood(self) -> np.ndarray:
        return self._de_test.log_likelihood

    def summary(self, nonnumeric=False, qval_thres=None, fc_upper_thres=None,
                fc_lower_thres=None, mean_thres=None) -> pd.DataFrame:
        """
        Summarize differential expression results into an output table.

        :param nonnumeric: Whether to include non-numeric covariates in fit.
        """
        # Collect summary from differential test object.
        res = self._de_test.summary()
        # Overwrite fold change with fold change from temporal model.
        # Note that log2_fold_change calls log_fold_change from this class
        # and not from the self._de_test object,
        # which is called by self._de_test.summary().
        res['log2fc'] = self.log2_fold_change()

        res = self._threshold_summary(
            res=res,
            qval_thres=qval_thres,
            fc_upper_thres=fc_upper_thres,
            fc_lower_thres=fc_lower_thres,
            mean_thres=mean_thres
        )

        return res

    def log_fold_change(self, base=np.e, genes=None, nonnumeric=False):
        """
        Return log_fold_change based on fitted expression values by gene.

        The log_fold_change is defined as the log of the fold change
        from the minimal to the maximal fitted value by gene.

        :param base: Basis of logarithm.
        :param genes: Genes for which to return maximum fitted value. Defaults
            to all genes if None.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Log-fold change of fitted expression value by gene.
        """
        if genes is None:
            genes = np.asarray(range(self.X.shape[1]))
        else:
            genes = self._idx_genes(genes)

        fc = self.max(genes=genes, nonnumeric=nonnumeric) - \
             self.min(genes=genes, nonnumeric=nonnumeric)
        fc = np.nextafter(0, 1, out=fc, where=fc == 0)

        return np.log(fc) / np.log(base)

    def _filter_genes_str(self, genes: list):
        """
        Filter genes indexed by ID strings by list of genes given in data set.

        :param genes: List of genes to filter.
        :return: Filtered list of genes
        """
        genes_found = np.array([x in self.gene_ids for x in genes])
        if any(genes_found == False):
            logger.info("did not find some genes, omitting")
            genes = genes[genes_found]
        return genes

    def _filter_genes_int(self, genes: list):
        """
        Filter genes indexed by integers by gene list length.

        :param genes: List of genes to filter.
        :return: Filtered list of genes
        """
        genes_found = np.array([x < self.X.shape[1] for x in genes])
        if any(genes_found == False):
            logger.info("did not find some genes, omitting")
            genes = genes[genes_found]
        return genes

    def _idx_genes(self, genes):
        if not isinstance(genes, list):
            if isinstance(genes, np.ndarray):
                genes = genes.tolist()
            else:
                genes = [genes]

        if isinstance(genes[0], str):
            genes = self._filter_genes_str(genes)
            genes = np.array([self.gene_ids.tolist().index(x) for x in genes])
        elif isinstance(genes[0], int) or isinstance(genes[0], np.int64):
            genes = self._filter_genes_int(genes)
        else:
            raise ValueError("only string and integer elements allowed in genes")
        return genes

    def _spline_par_loc_idx(self, intercept=True):
        """
        Get indices of spline basis model parameters in
        entire location parameter model parameter set.

        :param intercept: Whether to include intercept.
        :return: Indices of spline basis parameters of location model.
        """
        par_loc_names = self._model_estim.design_loc.coords['design_loc_params'].values.tolist()
        idx = [par_loc_names.index(x) for x in self._spline_coefs]
        if 'Intercept' in par_loc_names and intercept == True:
            idx = np.concatenate([np.where([[x == 'Intercept' for x in par_loc_names]])[0], idx])
        return idx

    def _continuous_model(self, idx, nonnumeric=False):
        """
        Recover continuous fit for a gene.

        :param idx: Index of genes to recover fit for.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Continuuos fit for each cell for given gene.
        """
        idx = np.asarray(idx)
        if nonnumeric:
            mu = np.matmul(self._model_estim.design_loc.values,
                           self._model_estim.par_link_loc[:,idx])
            if self._size_factors is not None:
                mu = mu + self._size_factors
        else:
            idx_basis = self._spline_par_loc_idx(intercept=True)
            mu = np.matmul(self._model_estim.design_loc[:,idx_basis].values,
                           self._model_estim.par_link_loc[idx_basis, idx])

        mu = np.exp(mu)
        return mu

    def max(self, genes, nonnumeric=False):
        """
        Return maximum fitted expression value by gene.

        :param genes: Genes for which to return maximum fitted value.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Maximum fitted expression value by gene.
        """
        genes = self._idx_genes(genes)
        return np.array([np.max(self._continuous_model(idx=i, nonnumeric=nonnumeric))
                         for i in genes])

    def min(self, genes, nonnumeric=False):
        """
        Return minimum fitted expression value by gene.

        :param genes: Genes for which to return maximum fitted value.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Maximum fitted expression value by gene.
        """
        genes = self._idx_genes(genes)
        return np.array([np.min(self._continuous_model(idx=i, nonnumeric=nonnumeric))
                         for i in genes])

    def argmax(self, genes, nonnumeric=False):
        """
        Return maximum fitted expression value by gene.

        :param genes: Genes for which to return maximum fitted value.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Maximum fitted expression value by gene.
        """
        genes = self._idx_genes(genes)
        idx = np.array([np.argmax(self._continuous_model(idx=i, nonnumeric=nonnumeric))
                        for i in genes])
        return self._continuous_coords[idx]

    def argmin(self, genes, nonnumeric=False):
        """
        Return minimum fitted expression value by gene.

        :param genes: Genes for which to return maximum fitted value.
        :param nonnumeric: Whether to include non-numeric covariates in fit.
        :return: Maximum fitted expression value by gene.
        """
        genes = self._idx_genes(genes)
        idx = np.array([np.argmin(self._continuous_model(idx=i, nonnumeric=nonnumeric))
                        for i in genes])
        return self._continuous_coords[idx]

    def plot_genes(
            self,
            genes,
            hue=None,
            size=1,
            log=True,
            nonnumeric=False,
            save=None,
            show=True,
            ncols=2,
            row_gap=0.3,
            col_gap=0.25,
            return_axs=False
    ):
        """
        Plot observed data and spline fits of selected genes.

        :param genes: Gene IDs to plot.
        :param hue: Confounder to include in plot.
        :param size: Point size.
        :param nonnumeric:
        :param save: Path+file name stem to save plots to.
            File will be save+"_genes.png". Does not save if save is None.
        :param show: Whether to display plot.
        :param ncols: Number of columns in plot grid if multiple genes are plotted.
        :param row_gap: Vertical gap between panel rows relative to panel height.
        :param col_gap: Horizontal gap between panel columns relative to panel width.
        :param return_axs: Whether to return axis objects of plots.
        :return: Matplotlib axis objects.
        """

        import seaborn as sns
        import matplotlib.pyplot as plt
        from matplotlib import gridspec
        from matplotlib import rcParams

        plt.ioff()

        gene_idx = self._idx_genes(genes)

        # Set up gridspec.
        ncols = ncols if len(gene_idx) > ncols else len(gene_idx)
        nrows = len(gene_idx) // ncols + (len(gene_idx) - (len(gene_idx) // ncols) * ncols)
        gs = gridspec.GridSpec(
            nrows=nrows,
            ncols=ncols,
            hspace=row_gap,
            wspace=col_gap
        )

        # Define figure size based on panel number and grid.
        fig = plt.figure(
            figsize=(
                ncols * rcParams['figure.figsize'][0],  # width in inches
                nrows * rcParams['figure.figsize'][1] * (1 + row_gap)  # height in inches
            )
        )

        # Build axis objects in loop.
        axs = []
        for i, g in enumerate(gene_idx):
            ax = plt.subplot(gs[i])
            axs.append(ax)

            y = self.X[:, g]
            yhat = self._continuous_model(idx=g, nonnumeric=nonnumeric)
            if log:
                y = np.log(y + 1)
                yhat = np.log(yhat + 1)

            sns.scatterplot(
                x=self._continuous_coords,
                y=y,
                hue=hue,
                size=size,
                ax=ax,
                legend=False
            )
            sns.lineplot(
                x=self._continuous_coords,
                y=yhat,
                hue=hue,
                ax=ax
            )

            ax.set_title(genes[i])
            ax.set_xlabel("continuous")
            if log:
                ax.set_ylabel("log expression")
            else:
                ax.set_ylabel("expression")

        # Save, show and return figure.
        if save is not None:
            plt.savefig(save+'_genes.png')

        if show:
            plt.show()

        plt.close(fig)

        if return_axs:
            return axs
        else:
            return

    def plot_heatmap(
            self,
            genes: Union[list, np.ndarray],
            save=None,
            show=True,
            transform: str = "zscore",
            nticks=10,
            cmap: str = "YlGnBu",
            width=10,
            height_per_gene=0.5,
            return_axs=False
    ):
        """
        Plot observed data and spline fits of selected genes.

        :param genes: Gene IDs to plot.
        :param save: Path+file name stem to save plots to.
            File will be save+"_genes.png". Does not save if save is None.
        :param show: Whether to display plot.
        :param transform: Gene-wise transform to use.
        :param nticks: Number of x ticks.
        :param cmap: matplotlib cmap.
        :param width: Width of heatmap figure.
        :param height_per_gene: Height of each row (gene) in heatmap figure.
        :param return_axs: Whether to return axis objects of plots.
        :return: Matplotlib axis objects.
        """
        import seaborn as sns
        import matplotlib.pyplot as plt

        plt.ioff()

        gene_idx = self._idx_genes(genes)

        # Define figure.
        fig = plt.figure(figsize=(width, height_per_gene * len(gene_idx)))
        ax = fig.add_subplot(111)

        # Build heatmap matrix.
        ## Add in data.
        data = np.array([
            self._continuous_model(idx=g, nonnumeric=False)
            for i, g in enumerate(gene_idx)
        ])
        ## Order columns by continuous covariate.
        idx_x_sorted = np.argsort(self._continuous_coords)
        data = data[:, idx_x_sorted]
        xcoord = self._continuous_coords[idx_x_sorted]

        if transform.lower() == "log10":
            data = np.nextafter(0, 1, out=data, where=data == 0)
            data = np.log(data) / np.log(10)
        elif transform.lower() == "zscore":
            mu = np.mean(data, axis=0)
            sd = np.std(data, axis=0)
            sd = np.nextafter(0, 1, out=sd, where=sd == 0)
            data = np.array([(x - mu[i]) / sd[i] for i, x in enumerate(data)])
        elif transform.lower() == "none":
            pass
        else:
            raise ValueError("transform not recognized in plot_heatmap()")

        # Create heatmap.
        sns.heatmap(data=data, cmap=cmap, ax=ax)

        # Set up axis labels.
        xtick_pos = np.asarray(np.round(np.linspace(
            start=0,
            stop=data.shape[1] - 1,
            num=nticks,
            endpoint=True
        )), dtype=int)
        xtick_lab = [str(np.round(xcoord[np.argmin(np.abs(xcoord - xcoord[i]))], 2))
                     for i in xtick_pos]
        ax.set_xticks(xtick_pos)
        ax.set_xticklabels(xtick_lab)
        ax.set_xlabel("continuous")
        plt.yticks(np.arange(len(genes)), genes, rotation='horizontal')
        ax.set_ylabel("genes")

        # Save, show and return figure.
        if save is not None:
            plt.savefig(save + '_genes.png')

        if show:
            plt.show()

        plt.close(fig)

        if return_axs:
            return axs
        else:
            return


class DifferentialExpressionTestWaldCont(_DifferentialExpressionTestCont):
    de_test: DifferentialExpressionTestWald

    def __init__(
            self,
            de_test: DifferentialExpressionTestWald,
            size_factors: np.ndarray,
            continuous_coords: np.ndarray,
            spline_coefs: list
    ):
        super().__init__(
            de_test=de_test,
            model_estim=de_test.model_estim,
            size_factors=size_factors,
            continuous_coords=continuous_coords,
            spline_coefs=spline_coefs
        )


class DifferentialExpressionTestLRTCont(_DifferentialExpressionTestCont):
    de_test: DifferentialExpressionTestLRT

    def __init__(self,
            de_test: DifferentialExpressionTestLRT,
            size_factors: np.ndarray,
            continuous_coords: np.ndarray,
            spline_coefs: list
    ):
        super().__init__(
            de_test=de_test,
            model_estim=de_test.full_estim,
            size_factors=size_factors,
            continuous_coords=continuous_coords,
            spline_coefs=spline_coefs
        )
