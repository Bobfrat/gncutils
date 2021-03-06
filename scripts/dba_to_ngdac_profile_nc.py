#!/usr/bin/env python

import os
import sys
import logging
import argparse
import tempfile
import datetime
import shutil
from gncutils.ProfileNetCDFWriter import ProfileNetCDFWriter
from gncutils.readers.dba import create_llat_dba_reader
from gncutils.yo import slice_sensor_data, find_profiles
from gncutils.constants import NETCDF_FORMATS, LLAT_SENSORS
from gncutils.validate import validate_sensors, validate_ngdac_var_names
import numpy as np
from gncutils.ctd import calculate_practical_salinity, calculate_density, calculate_depth


def main(args):
    """Parse one or more Slocum glider ascii dba files and write IOOS NGDAC-compliant Profile NetCDF files
    """

    # Set up logger
    log_level = getattr(logging, args.loglevel.upper())
    log_format = '%(module)s:%(levelname)s:%(message)s [line %(lineno)d]'
    logging.basicConfig(format=log_format, level=log_level)

    config_path = args.config_path
    output_path = args.output_path or os.path.realpath(os.curdir)
    dba_files = args.dba_files
    start_profile_id = args.start_profile_id
    clobber = args.clobber
    comp_level = args.compression
    nc_format = args.nc_format
    ctd_sensor_prefix = args.ctd_sensor_prefix

    ctd_sensor_names = ['{:s}_{:s}'.format(ctd_sensor_prefix, ctd_sensor) for ctd_sensor in
                        ['water_cond', 'water_temp']]
    ctd_sensors = LLAT_SENSORS + ctd_sensor_names

    if not os.path.isdir(config_path):
        logging.error('Invalid configuration directory: {:s}'.format(config_path))
        return 1

    if not output_path:
        args.output_path = os.path.realpath(os.curdir)
        logging.info('No NetCDF output_path specified. Using cwd: {:s}'.format(output_path))

    if not os.path.isdir(output_path):
        logging.error('Invalid output_path: {:s}'.format(output_path))
        return 1

    if not dba_files:
        logging.error('No Slocum dba files specified')
        return 1

    # Create the Trajectory NetCDF writer
    ncw = ProfileNetCDFWriter(config_path, comp_level=comp_level, nc_format=nc_format, profile_id=start_profile_id,
                              clobber=clobber)
    # Make sure we have llat_* sensors defined in ncw.nc_sensor_defs
    ctd_valid = validate_sensors(ncw.nc_sensor_defs, ctd_sensors)
    if not ctd_valid:
        logging.error('Bad sensor definitions: {:s}'.format(ncw.sensor_defs_file))
        return 1
    # Make sure we have configured sensor definitions for all IOOS NGDAC required variables
    ngdac_valid = validate_ngdac_var_names(ncw.nc_sensor_defs)
    if not ngdac_valid:
        logging.error('Bad sensor definitions: {:s}'.format(ncw.sensor_defs_file))
        return 1

    if args.debug:
        sys.stdout.write('{}\n'.format(ncw))
        return 0

    # Create a temporary directory for creating/writing NetCDF prior to moving them to output_path
    tmp_dir = tempfile.mkdtemp()
    logging.debug('Temporary NetCDF directory: {:s}'.format(tmp_dir))

    # Write one NetCDF file for each input file
    output_nc_files = []
    processed_dbas = []
    for dba_file in dba_files:

        if not os.path.isfile(dba_file):
            logging.error('Invalid dba file specified: {:s}'.format(dba_file))
            continue

        logging.info('Processing dba file: {:s}'.format(dba_file))

        # Parse the dba file
        dba = create_llat_dba_reader(dba_file)
        if len(dba['data']) == 0:
            logging.warning('Skipping empty dba file: {:s}'.format(dba_file))
            continue

        # Create the yo for profile indexing find the profile minima/maxima
        yo = slice_sensor_data(dba)
        if yo is None:
            continue
        try:
            profile_times = find_profiles(yo)
        except ValueError as e:
            logging.error('{:s}: {:s}'.format(dba_file, e))
            continue

        if len(profile_times) == 0:
            logging.info('No profiles indexed: {:s}'.format(dba_file))
            continue

        # Pull out llat_time, llat_pressure, llat_lat, llat_lon, sci_water_temp and sci_water_cond to calculate:
        # - depth
        # - salinity
        # - density
        ctd_data = slice_sensor_data(dba, sensors=ctd_sensors)
        # Make sure we have latitudes and longitudes
        if np.all(np.isnan(ctd_data[:, 2])):
            logging.warning('dba contains no valid llat_latitude values'.format(dba_file))
            logging.info('Skipping dba: {:s}'.format(dba_file))
            continue
        if np.all(np.isnan(ctd_data[:, 3])):
            logging.warning('dba contains no valid llat_longitude values'.format(dba_file))
            logging.info('Skipping dba: {:s}'.format(dba_file))
            continue
        # Calculate mean llat_latitude and mean llat_longitude
        mean_lat = np.nanmean(ctd_data[:, 2])
        mean_lon = np.nanmean(ctd_data[:, 3])
        # Calculate practical salinity
        prac_sal = calculate_practical_salinity(ctd_data[:, 5], ctd_data[:, 6], ctd_data[:, 1])
        # Add salinity to the dba
        dba['sensors'].append({'attrs': ncw.nc_sensor_defs['salinity']['attrs'], 'sensor_name': 'salinity'})
        dba['data'] = np.append(dba['data'], np.expand_dims(prac_sal, axis=1), axis=1)

        # Calculate density
        density = calculate_density(ctd_data[:, 0], ctd_data[:, 5], ctd_data[:, 1], prac_sal, mean_lat, mean_lon)
        # Add density to the dba
        dba['sensors'].append({'attrs': ncw.nc_sensor_defs['density']['attrs'], 'sensor_name': 'density'})
        dba['data'] = np.append(dba['data'], np.expand_dims(density, axis=1), axis=1)

        # Calculate depth from pressure and replace the old llat_depth
        zi = [s['sensor_name'] for s in dba['sensors']].index('llat_depth')
        dba['data'][:, zi] = calculate_depth(ctd_data[:, 1], mean_lat)

        # All timestamps from stream
        ts = yo[:, 0]

        for profile_interval in profile_times:

            # Profile start time
            p0 = profile_interval[0]
            # Profile end time
            p1 = profile_interval[-1]
            # Find all rows in ts that are between p0 & p1
            p_inds = np.flatnonzero(np.logical_and(ts >= p0, ts <= p1))
            # profile_stream = dba['data'][p_inds[0]:p_inds[-1]]

            # Calculate and convert profile mean time to a datetime
            mean_profile_epoch = np.nanmean(profile_interval)
            if np.isnan(mean_profile_epoch):
                logging.warning('Profile mean timestamp is Nan')
                continue
            # If no start profile id was specified on the command line, use the mean_profile_epoch as the profile_id
            # since it will be unique to this profile and deployment
            if args.start_profile_id < 1:
                ncw.profile_id = int(mean_profile_epoch)
            pro_mean_dt = datetime.datetime.utcfromtimestamp(mean_profile_epoch)

            # Create the output NetCDF path
            pro_mean_ts = pro_mean_dt.strftime('%Y%m%dT%H%M%SZ')
            profile_filename = '{:s}-{:s}-{:s}-profile'.format(ncw.attributes['deployment']['glider'], pro_mean_ts,
                                                               dba['file_metadata']['filename_extension'])
            # Path to temporarily hold file while we create it
            tmp_fid, tmp_nc = tempfile.mkstemp(dir=tmp_dir, suffix='.nc', prefix=os.path.basename(profile_filename))
            os.close(tmp_fid)

            out_nc_file = os.path.join(output_path, '{:s}.nc'.format(profile_filename))
            if os.path.isfile(out_nc_file):
                if args.clobber:
                    logging.info('Clobbering existing NetCDF: {:s}'.format(out_nc_file))
                else:
                    logging.warning('Skipping existing NetCDF: {:s}'.format(out_nc_file))
                    continue

            # Initialize the temporary NetCDF file
            try:
                ncw.init_nc(tmp_nc)
            except (OSError, IOError) as e:
                logging.error('Error initializing {:s}: {}'.format(tmp_nc, e))
                continue

            try:
                ncw.open_nc()
                # Add command line call used to create the file
                ncw.update_history('{:s} {:s}'.format(sys.argv[0], dba_file))
            except (OSError, IOError) as e:
                logging.error('Error opening {:s}: {}'.format(tmp_nc, e))
                os.unlink(tmp_nc)
                continue

            # Create and set the trajectory
            trajectory_string = '{:s}'.format(ncw.trajectory)
            ncw.set_trajectory_id()
            # Update the global title attribute with the name of the source dba file
            ncw.set_title('{:s}-{:s} Vertical Profile'.format(ncw.deployment_configs['glider'],
                                                              pro_mean_dt.strftime('%Y%m%d%H%M%SZ')))

            # Create the source file scalar variable
            ncw.set_source_file_var(dba['file_metadata']['filename_label'], dba['file_metadata'])

            # Update the self.nc_sensors_defs with the dba sensor definitions
            ncw.update_data_file_sensor_defs(dba['sensors'])

            # Find and set container variables
            ncw.set_container_variables()

            # Create variables and add data
            for v in list(range(len(dba['sensors']))):
                var_name = dba['sensors'][v]['sensor_name']
                var_data = dba['data'][p_inds, v]
                logging.debug('Inserting {:s} data array'.format(var_name))

                ncw.insert_var_data(var_name, var_data)

            # Write scalar profile variable and permanently close the NetCDF file
            nc_file = ncw.finish_nc()

            if nc_file:
                try:
                    shutil.move(tmp_nc, out_nc_file)
                    os.chmod(out_nc_file, 0o755)
                except IOError as e:
                    logging.error('Error moving temp NetCDF file {:s}: {:}'.format(tmp_nc, e))
                    continue

            output_nc_files.append(out_nc_file)

        processed_dbas.append(dba_file)

    # Delete the temporary directory once files have been moved
    try:
        logging.debug('Removing temporary directory: {:s}'.format(tmp_dir))
        shutil.rmtree(tmp_dir)
    except OSError as e:
        logging.error(e)
        return 1

    # Print the list of files created
    for output_nc_file in output_nc_files:
        os.chmod(output_nc_file, 0o664)
        sys.stdout.write('{:s}\n'.format(output_nc_file))

    return 0


if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser(description=main.__doc__,
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    arg_parser.add_argument('config_path',
                            help='Location of deployment configuration files')

    arg_parser.add_argument('dba_files',
                            help='Source ASCII dba files to process',
                            nargs='+')

    arg_parser.add_argument('--ctd_sensor_prefix',
                            help='Specify Slocum glider ctd sensor prefix letter, i.e.: sci_water_temp, sci_water_cond',
                            choices=['sci', 'm'],
                            default='sci')

    arg_parser.add_argument('-p', '--start_profile_id',
                            help='Integer specifying the beginning profile id. If not specified or <1 the mean profile unix timestamp is used',
                            type=int,
                            default=0)

    arg_parser.add_argument('-o', '--output_path',
                            help='NetCDF destination directory, which must exist. Current directory if not specified')

    arg_parser.add_argument('-c', '--clobber',
                            help='Clobber existing NetCDF files if they exist',
                            action='store_true')

    arg_parser.add_argument('-f', '--format',
                            dest='nc_format',
                            help='NetCDF file format',
                            choices=NETCDF_FORMATS,
                            default='NETCDF4_CLASSIC')

    arg_parser.add_argument('--compression',
                            help='NetCDF4 compression level',
                            choices=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                            default=1)

    arg_parser.add_argument('-x', '--debug',
                            help='Check configuration and create NetCDF file writer, but does not process any files',
                            action='store_true')

    arg_parser.add_argument('-l', '--loglevel',
                            help='Verbosity level',
                            type=str,
                            choices=['debug', 'info', 'warning', 'error', 'critical'],
                            default='info')

    parsed_args = arg_parser.parse_args()

    sys.exit(main(parsed_args))
