import numpy as np
from scipy.optimize import differential_evolution

from .base import GravitationalWaveTransient
from ...core.utils import logger
from ...core.prior.base import Constraint
from ...core.prior import DeltaFunction
from ..utils import noise_weighted_inner_product


class RelativeBinningGravitationalWaveTransient(GravitationalWaveTransient):
    """A gravitational-wave transient likelihood object which uses the relative
    binning procedure to calculate a fast likelihood. See IAS paper:


    Parameters
    ----------
    interferometers: list, bilby.gw.detector.InterferometerList
        A list of `bilby.detector.Interferometer` instances - contains the
        detector data and power spectral densities
    waveform_generator: `bilby.waveform_generator.WaveformGenerator`
        An object which computes the frequency-domain strain of the signal,
        given some set of parameters
    fiducial_parameters: dict, optional
        A starting guess for initial parameters of the event for finding the
        maximum likelihood (fiducial) waveform.
    parameter_bounds: dict, optional
        Dictionary of bounds (lists) for the initial parameters when finding
        the initial maximum likelihood (fiducial) waveform.
    distance_marginalization: bool, optional
        If true, marginalize over distance in the likelihood.
        This uses a look up table calculated at run time.
        The distance prior is set to be a delta function at the minimum
        distance allowed in the prior being marginalised over.
    time_marginalization: bool, optional
        If true, marginalize over time in the likelihood.
        This uses a FFT to calculate the likelihood over a regularly spaced
        grid.
        In order to cover the whole space the prior is set to be uniform over
        the spacing of the array of times.
        If using time marginalisation and jitter_time is True a "jitter"
        parameter is added to the prior which modifies the position of the
        grid of times.
    phase_marginalization: bool, optional
        If true, marginalize over phase in the likelihood.
        This is done analytically using a Bessel function.
        The phase prior is set to be a delta function at phase=0.
    priors: dict, optional
        If given, used in the distance and phase marginalization.
    distance_marginalization_lookup_table: (dict, str), optional
        If a dict, dictionary containing the lookup_table, distance_array,
        (distance) prior_array, and reference_distance used to construct
        the table.
        If a string the name of a file containing these quantities.
        The lookup table is stored after construction in either the
        provided string or a default location:
        '.distance_marginalization_lookup_dmin{}_dmax{}_n{}.npz'
    jitter_time: bool, optional
        Whether to introduce a `time_jitter` parameter. This avoids either
        missing the likelihood peak, or introducing biases in the
        reconstructed time posterior due to an insufficient sampling frequency.
        Default is False, however using this parameter is strongly encouraged.
    reference_frame: (str, bilby.gw.detector.InterferometerList, list), optional
        Definition of the reference frame for the sky location.
        - "sky": sample in RA/dec, this is the default
        - e.g., "H1L1", ["H1", "L1"], InterferometerList(["H1", "L1"]):
          sample in azimuth and zenith, `azimuth` and `zenith` defined in the
          frame where the z-axis is aligned the the vector connecting H1
          and L1.
    time_reference: str, optional
        Name of the reference for the sampled time parameter.
        - "geocent"/"geocenter": sample in the time at the Earth's center,
          this is the default
        - e.g., "H1": sample in the time of arrival at H1
    chi: float, optional
        Tunable parameter which limits the perturbation of alpha when setting
        up the bin range. See https://arxiv.org/abs/1806.08792.
    epsilon: float, optional
        Tunable parameter which limits the differential phase change in each
        bin when setting up the bin range. See https://arxiv.org/abs/1806.08792.

    Returns
    -------
    Likelihood: `bilby.core.likelihood.Likelihood`
        A likelihood object, able to compute the likelihood of the data given
        some model parameters.
    """

    def __init__(self, interferometers,
                 waveform_generator,
                 fiducial_parameters=None,
                 parameter_bounds=None,
                 maximization_kwargs=None,
                 update_fiducial_parameters=False,
                 distance_marginalization=False,
                 time_marginalization=False,
                 phase_marginalization=False,
                 priors=None,
                 distance_marginalization_lookup_table=None,
                 jitter_time=True,
                 reference_frame="sky",
                 time_reference="geocenter",
                 chi=1,
                 epsilon=0.5):

        super(RelativeBinningGravitationalWaveTransient, self).__init__(
            interferometers=interferometers,
            waveform_generator=waveform_generator,
            distance_marginalization=distance_marginalization,
            phase_marginalization=phase_marginalization,
            time_marginalization=time_marginalization,
            priors=priors,
            distance_marginalization_lookup_table=distance_marginalization_lookup_table,
            jitter_time=jitter_time,
            reference_frame=reference_frame,
            time_reference=time_reference)

        if fiducial_parameters is None:
            fiducial_parameters = dict()
        self.fiducial_parameters = fiducial_parameters
        self.chi = chi
        self.epsilon = epsilon
        self.gamma = np.array([-5 / 3, -2 / 3, 1, 5 / 3, 7 / 3])
        self.fiducial_waveform_obtained = False
        self.check_if_bins_are_setup = False
        self.fiducial_polarizations = None
        self.per_detector_fiducial_waveforms = dict()
        self.per_detector_fiducial_waveform_points = dict()
        self.bin_freqs = dict()
        self.bin_inds = dict()
        self.bin_widths = dict()
        self.bin_centers = dict()
        self.set_fiducial_waveforms(self.fiducial_parameters)
        logger.info("Initial fiducial waveforms set up")
        self.setup_bins()
        self.compute_summary_data()
        logger.info("Summary Data Obtained")

        if update_fiducial_parameters:
            # write a check to make sure prior is not None
            logger.info("Using scipy optimization to find maximum likelihood parameters.")
            self.parameters_to_be_updated = [key for key in self.priors if not isinstance(
                self.priors[key], (DeltaFunction, Constraint))]
            logger.info("Parameters over which likelihood is maximized: {}".format(self.parameters_to_be_updated))
            if parameter_bounds is None:
                logger.info("No parameter bounds were given. Using priors instead.")
                self.parameter_bounds = self.get_bounds_from_priors(self.priors)
            else:
                self.parameter_bounds = self.get_parameter_list_from_dictionary(parameter_bounds)
            self.fiducial_parameters = self.find_maximum_likelihood_parameters(
                self.parameter_bounds, maximization_kwargs=maximization_kwargs)

    def __repr__(self):
        return self.__class__.__name__ + '(interferometers={},\n\twaveform_generator={},\n\fiducial_parameters={},' \
            .format(self.interferometers, self.waveform_generator, self.fiducial_parameters)

    def setup_bins(self):
        frequency_array = self.waveform_generator.frequency_array
        gamma = self.gamma
        for interferometer in self.interferometers:
            name = interferometer.name
            frequency_array_useful = frequency_array[np.intersect1d(
                np.where(frequency_array >= interferometer.minimum_frequency),
                np.where(frequency_array <= interferometer.maximum_frequency))]

            d_alpha = self.chi * 2 * np.pi / np.abs(
                (interferometer.minimum_frequency ** gamma) * np.heaviside(
                    -gamma, 1) - (interferometer.maximum_frequency ** gamma) * np.heaviside(
                    gamma, 1))
            d_phi = np.sum(np.array([np.sign(gamma[i]) * d_alpha[i] * (
                frequency_array_useful ** gamma[i]) for i in range(len(gamma))]), axis=0)
            d_phi_from_start = d_phi - d_phi[0]
            self.number_of_bins = int(d_phi_from_start[-1] // self.epsilon)
            self.bin_freqs[name] = np.zeros(self.number_of_bins + 1)
            self.bin_inds[name] = np.zeros(self.number_of_bins + 1, dtype=int)

            for i in range(self.number_of_bins + 1):
                bin_index = np.where(d_phi_from_start >= ((i / self.number_of_bins) * d_phi_from_start[-1]))[0][0]
                bin_freq = frequency_array_useful[bin_index]
                self.bin_freqs[interferometer.name][i] = bin_freq
                self.bin_inds[interferometer.name][i] = np.where(frequency_array >= bin_freq)[0][0]

            logger.info("Set up {} bins for {} between {} Hz and {} Hz".format(
                self.number_of_bins, interferometer.name, interferometer.minimum_frequency,
                interferometer.maximum_frequency))
            self.waveform_generator.waveform_arguments["frequency_bin_edges"] = self.bin_freqs[interferometer.name]
            self.bin_widths[name] = self.bin_freqs[name][1:] - self.bin_freqs[name][:-1]
            self.bin_centers[name] = (self.bin_freqs[name][1:] + self.bin_freqs[name][:-1]) / 2
            self.per_detector_fiducial_waveform_points[name] = (
                self.per_detector_fiducial_waveforms[name][self.bin_inds[name]]
            )
        return

    def set_fiducial_waveforms(self, parameters):
        parameters["fiducial"] = 1
        self.fiducial_polarizations = self.waveform_generator.frequency_domain_strain(
            parameters)

        maximum_nonzero_index = np.where(self.fiducial_polarizations["plus"] != 0j)[0][-1]
        logger.info("Maximum Nonzero Index is {}".format(maximum_nonzero_index))
        maximum_nonzero_frequency = self.waveform_generator.frequency_array[maximum_nonzero_index]
        logger.info("Maximum Nonzero Frequency is {}".format(maximum_nonzero_frequency))

        if self.fiducial_polarizations is None:
            return np.nan_to_num(-np.inf)

        for interferometer in self.interferometers:
            logger.info("Maximum Frequency is {}".format(interferometer.maximum_frequency))
            if interferometer.maximum_frequency > maximum_nonzero_frequency:
                interferometer.maximum_frequency = maximum_nonzero_frequency

            self.per_detector_fiducial_waveforms[interferometer.name] = (
                interferometer.get_detector_response(
                    self.fiducial_polarizations, parameters))
        parameters["fiducial"] = 0
        return

    def log_likelihood(self):
        return self.log_likelihood_ratio() + self.noise_log_likelihood()

    def find_maximum_likelihood_parameters(self, parameter_bounds,
                                           iterations=1, maximization_kwargs=None):
        if maximization_kwargs is None:
            maximization_kwargs = dict()
        for i in range(iterations):
            logger.info("Optimizing fiducial parameters. Iteration : {}".format(i + 1))
            output = differential_evolution(self.lnlike_scipy_maximize,
                                            bounds=parameter_bounds, **maximization_kwargs)
            updated_parameters_list = output['x']
            updated_parameters = self.get_parameter_dictionary_from_list(updated_parameters_list)
            self.set_fiducial_waveforms(updated_parameters)
            self.compute_summary_data()

        logger.info("Fiducial waveforms updated")
        logger.info("Summary Data updated")
        return updated_parameters

    def lnlike_scipy_maximize(self, parameter_list):
        self.parameters = self.get_parameter_dictionary_from_list(parameter_list)
        return -self.log_likelihood_ratio()

    def get_parameter_dictionary_from_list(self, parameter_list):
        parameter_dictionary = dict(zip(self.parameters_to_be_updated, parameter_list))
        excluded_parameter_keys = set(self.fiducial_parameters) - set(self.parameters_to_be_updated)
        for key in excluded_parameter_keys:
            parameter_dictionary[key] = self.fiducial_parameters[key]
        return parameter_dictionary

    def get_parameter_list_from_dictionary(self, parameter_dict):
        return [parameter_dict[k] for k in self.parameters_to_be_updated]

    def get_bounds_from_priors(self, priors):
        bounds = []
        for key in self.parameters_to_be_updated:
            bounds.append([priors[key].minimum, priors[key].maximum])
        return bounds

    def compute_summary_data(self):
        summary_data = dict()

        for interferometer in self.interferometers:
            mask = interferometer.frequency_mask
            masked_frequency_array = interferometer.frequency_array[mask]
            masked_bin_inds = []
            for edge in self.bin_freqs[interferometer.name]:
                index = np.where(masked_frequency_array == edge)[0][0]
                masked_bin_inds.append(index)
            masked_strain = interferometer.frequency_domain_strain[mask]
            masked_h0 = self.per_detector_fiducial_waveforms[interferometer.name][mask]
            masked_psd = interferometer.power_spectral_density_array[mask]
            a0, b0, a1, b1 = np.zeros((4, self.number_of_bins), dtype=complex)

            for i in range(self.number_of_bins):

                central_frequency_i = 0.5 * \
                    (masked_frequency_array[masked_bin_inds[i]] + masked_frequency_array[masked_bin_inds[i + 1]])
                masked_strain_i = masked_strain[masked_bin_inds[i]:masked_bin_inds[i + 1]]
                masked_h0_i = masked_h0[masked_bin_inds[i]:masked_bin_inds[i + 1]]
                masked_psd_i = masked_psd[masked_bin_inds[i]:masked_bin_inds[i + 1]]
                masked_frequency_i = masked_frequency_array[masked_bin_inds[i]:masked_bin_inds[i + 1]]

                a0[i] = noise_weighted_inner_product(
                    masked_h0_i,
                    masked_strain_i,
                    masked_psd_i,
                    self.waveform_generator.duration)

                b0[i] = noise_weighted_inner_product(
                    masked_h0_i,
                    masked_h0_i,
                    masked_psd_i,
                    self.waveform_generator.duration)

                a1[i] = noise_weighted_inner_product(
                    masked_h0_i,
                    masked_strain_i * (masked_frequency_i - central_frequency_i),
                    masked_psd_i,
                    self.waveform_generator.duration)

                b1[i] = noise_weighted_inner_product(
                    masked_h0_i,
                    masked_h0_i * (masked_frequency_i - central_frequency_i),
                    masked_psd_i,
                    self.waveform_generator.duration)

            summary_data[interferometer.name] = (a0, a1, b0, b1)

        self.summary_data = summary_data

    def compute_waveform_ratio_per_interferometer(self, waveform_polarizations, interferometer):
        name = interferometer.name
        strain = interferometer.get_detector_response(
            waveform_polarizations=waveform_polarizations,
            parameters=self.parameters,
            frequencies=self.bin_freqs[interferometer.name],
        )
        reference_strain = self.per_detector_fiducial_waveform_points[name]
        waveform_ratio = strain / reference_strain

        r0 = (waveform_ratio[1:] + waveform_ratio[:-1]) / 2
        r1 = (waveform_ratio[1:] - waveform_ratio[:-1]) / self.bin_widths[name]

        return [r0, r1]

    def compute_waveform_ratio(self, waveform_polarizations):
        waveform_ratio = dict()
        for interferometer in self.interferometers:
            waveform_ratio[interferometer.name] = self.compute_waveform_ratio_per_interferometer(
                waveform_polarizations=waveform_polarizations,
                interferometer=interferometer,
            )
        return waveform_ratio

    def _compute_full_waveform(self, signal_polarizations, interferometer):
        fiducial_waveform = self.per_detector_fiducial_waveforms[interferometer.name]
        r0, r1 = self.compute_waveform_ratio_per_interferometer(
            waveform_polarizations=signal_polarizations,
            interferometer=interferometer,
        )
        ind = self.bin_inds[interferometer.name]
        f = interferometer.frequency_array
        duplicated_r0, duplicated_r1, duplicated_fm = np.zeros((3, f.shape[0]), dtype=complex)

        for i in range(self.number_of_bins):
            fm = self.bin_centers[interferometer.name]
            duplicated_fm[ind[i]:ind[i + 1]] = fm[i]
            duplicated_r0[ind[i]:ind[i + 1]] = r0[i]
            duplicated_r1[ind[i]:ind[i + 1]] = r1[i]

        full_waveform_ratio = duplicated_r0 + duplicated_r1 * (f - duplicated_fm)
        return fiducial_waveform * full_waveform_ratio

    def calculate_snrs(self, waveform_polarizations, interferometer):
        waveform_ratio_per_detector = self.compute_waveform_ratio_per_interferometer(
            waveform_polarizations=waveform_polarizations,
            interferometer=interferometer,
        )
        a0, a1, b0, b1 = self.summary_data[interferometer.name]

        r0, r1 = waveform_ratio_per_detector

        d_inner_h = np.sum(a0 * np.conjugate(r0) + a1 * np.conjugate(r1))
        h_inner_h = np.sum(b0 * np.abs(r0) ** 2 + 2 * b1 * np.real(r0 * np.conjugate(r1)))
        optimal_snr_squared = h_inner_h
        complex_matched_filter_snr = d_inner_h / (optimal_snr_squared ** 0.5)

        if self.time_marginalization:
            full_waveform = self._compute_full_waveform(
                signal_polarizations=waveform_polarizations,
                interferometer=interferometer,
            )
            d_inner_h_array = 4 / self.waveform_generator.duration * np.fft.fft(
                full_waveform[0:-1]
                * interferometer.frequency_domain_strain.conjugate()[0:-1]
                / interferometer.power_spectral_density_array[0:-1])

        else:
            d_inner_h_array = None

        return self._CalculatedSNRs(
            d_inner_h=d_inner_h, optimal_snr_squared=optimal_snr_squared,
            complex_matched_filter_snr=complex_matched_filter_snr,
            d_inner_h_array=d_inner_h_array, optimal_snr_squared_array=None,
            d_inner_h_squared_tc_array=None)
