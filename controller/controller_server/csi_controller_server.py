import grpc
import time
from optparse import OptionParser
import yaml
import os.path

from concurrent import futures
from controller.csi_general import csi_pb2
from controller.csi_general import csi_pb2_grpc
from controller.array_action.array_connection_manager import ArrayConnectionManager
from controller.common.csi_logger import get_stdout_logger
from controller.common.csi_logger import set_log_level
import controller.controller_server.config as config
import controller.controller_server.utils as utils
import controller.array_action.errors as controller_errors
from controller.controller_server.errors import ValidationException
from controller.common.utils import set_current_thread_name
from controller.common.node_info import NodeIdInfo
from controller.array_action.array_mediator_action import map_volume, unmap_volume

logger = None #is set in ControllerServicer::__init__


class ControllerServicer(csi_pb2_grpc.ControllerServicer):
    """
    gRPC server for Digestor Service
    """

    def __init__(self, array_endpoint):
		# init logger
        global logger
        logger = get_stdout_logger()

        self.endpoint = array_endpoint

        my_path = os.path.abspath(os.path.dirname(__file__))
        path = os.path.join(my_path, "../../common/config.yaml")

        with open(path, 'r') as yamlfile:
            self.cfg = yaml.safe_load(yamlfile)  # TODO: add the following when possible : Loader=yaml.FullLoader)

    def CreateVolume(self, request, context):
        set_current_thread_name(request.name)
        logger.info("create volume")
        try:
            utils.validate_create_volume_request(request)
        except ValidationException as ex:
            logger.error("failed request validation")
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            return csi_pb2.CreateVolumeResponse()

        volume_name = request.name
        logger.debug("volume name : {}".format(volume_name))

        secrets = request.secrets
        user, password, array_addresses = utils.get_array_connection_info_from_secret(secrets)

        pool = request.parameters[config.PARAMETERS_POOL]
        capabilities = {
            key: value for key, value in request.parameters.items() if key in [
                config.PARAMETERS_CAPABILITIES_SPACEEFFICIENCY,
            ]
        }

        if config.PARAMETERS_PREFIX in request.parameters:
            volume_prefix = request.parameters[config.PARAMETERS_PREFIX]
            volume_name = volume_prefix + "_" + volume_name

        try:
            # TODO : pass multiple array addresses
            with ArrayConnectionManager(user, password, array_addresses) as array_mediator:
                logger.debug(array_mediator)

                if len(volume_name) > array_mediator.max_vol_name_length:
                    volume_name = volume_name[:array_mediator.max_vol_name_length]
                    logger.warning("volume name is too long - cutting it to be of size : {0}. new name : {1}".format(
                        array_mediator.max_vol_name_length, volume_name))

                size = request.capacity_range.required_bytes

                if size == 0:
                    size = array_mediator.minimal_volume_size_in_bytes
                    logger.debug("requested size is 0 so the default size will be used : {0} ".format(
                        size))
                try:
                    vol = array_mediator.get_volume(volume_name)

                except controller_errors.VolumeNotFoundError as ex:
                    logger.debug(
                        "volume was not found. creating a new volume with parameters: {0}".format(request.parameters))

                    array_mediator.validate_supported_capabilities(capabilities)
                    vol = array_mediator.create_volume(volume_name, size, capabilities, pool)

                else:
                    logger.debug("volume found : {}".format(vol))

                    if not (vol.capacity_bytes == request.capacity_range.required_bytes):
                        context.set_details("Volume was already created with different size.")
                        context.set_code(grpc.StatusCode.ALREADY_EXISTS)
                        return csi_pb2.CreateVolumeResponse()

                logger.debug("generating create volume response")
                res = utils.generate_csi_create_volume_response(vol)
                logger.info("finished create volume")
                return res

        except (controller_errors.IllegalObjectName, controller_errors.StorageClassCapabilityNotSupported,
                controller_errors.PoolDoesNotExist, controller_errors.PoolDoesNotMatchCapabilities) as ex:
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            return csi_pb2.CreateVolumeResponse()
        except controller_errors.PermissionDeniedError as ex:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(ex)
            return csi_pb2.CreateVolumeResponse()
        except controller_errors.VolumeAlreadyExists as ex:
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            return csi_pb2.CreateVolumeResponse()
        except Exception as ex:
            logger.error("an internal exception occurred")
            logger.exception(ex)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('an internal exception occurred : {}'.format(ex))
            return csi_pb2.CreateVolumeResponse()

    def DeleteVolume(self, request, context):
        set_current_thread_name(request.volume_id)
        logger.info("DeleteVolume")
        secrets = request.secrets

        try:
            utils.validate_delete_volume_request(request)

            user, password, array_addresses = utils.get_array_connection_info_from_secret(secrets)

            try:
                array_type, vol_id = utils.get_volume_id_info(request.volume_id)
            except controller_errors.VolumeNotFoundError as ex:
                logger.warning("volume id is invalid. error : {}".format(ex))
                return csi_pb2.DeleteVolumeResponse()

            with ArrayConnectionManager(user, password, array_addresses, array_type) as array_mediator:

                logger.debug(array_mediator)

                try:
                    array_mediator.delete_volume(vol_id)

                except controller_errors.VolumeNotFoundError as ex:
                    logger.debug("volume was not found during deletion: {0}".format(ex))

                except controller_errors.PermissionDeniedError as ex:
                    context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                    context.set_details(ex)
                    return csi_pb2.DeleteVolumeResponse()

        except ValidationException as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            return csi_pb2.DeleteVolumeResponse()

        except Exception as ex:
            logger.debug("an internal exception occurred")
            logger.exception(ex)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('an internal exception occurred : {}'.format(ex))
            return csi_pb2.DeleteVolumeResponse()

        logger.debug("generating delete volume response")
        res = csi_pb2.DeleteVolumeResponse()
        logger.info("finished DeleteVolume")
        return res

    def ControllerPublishVolume(self, request, context):
        set_current_thread_name(request.volume_id)
        logger.info("ControllerPublishVolume")
        try:
            utils.validate_publish_volume_request(request)

            array_type, vol_id = utils.get_volume_id_info(request.volume_id)

            node_id_info = NodeIdInfo(request.node_id)
            node_name = node_id_info.node_name
            initiators = node_id_info.initiators

            logger.debug("node name for this publish operation is : {0}".format(node_name))

            user, password, array_addresses = utils.get_array_connection_info_from_secret(request.secrets)
            lun, connectivity_type, array_initiators = map_volume(user, password, array_addresses, array_type, vol_id, initiators)

            logger.info("finished ControllerPublishVolume")
            res = utils.generate_csi_publish_volume_response(lun,
                                                         connectivity_type,
                                                         self.cfg,
                                                         array_initiators)
            return res

        except controller_errors.VolumeMappedToMultipleHostsError as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            return csi_pb2.ControllerPublishVolumeResponse()

        except controller_errors.PermissionDeniedError as ex:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(ex)
            return csi_pb2.ControllerPublishVolumeResponse()

        except (controller_errors.LunAlreadyInUseError, controller_errors.NoAvailableLunError) as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
            return csi_pb2.ControllerPublishVolumeResponse()

        except (controller_errors.HostNotFoundError, controller_errors.VolumeNotFoundError, controller_errors.BadNodeIdError) as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return csi_pb2.ControllerPublishVolumeResponse()

        except ValidationException as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            return csi_pb2.ControllerPublishVolumeResponse()

        except Exception as ex:
            logger.debug("an internal exception occurred")
            logger.exception(ex)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('an internal exception occurred : {}'.format(ex))
            return csi_pb2.ControllerPublishVolumeResponse()

    def ControllerUnpublishVolume(self, request, context):
        set_current_thread_name(request.volume_id)
        logger.info("ControllerUnpublishVolume")
        try:
            try:
                utils.validate_unpublish_volume_request(request)
            except ValidationException as ex:
                logger.exception(ex)
                context.set_details(ex.message)
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                return csi_pb2.ControllerUnpublishVolumeResponse()

            array_type, vol_id = utils.get_volume_id_info(request.volume_id)

            node_id_info = NodeIdInfo(request.node_id)
            node_name = node_id_info.node_name
            initiators = node_id_info.initiators
            logger.debug("node name for this unpublish operation is : {0}".format(node_name))

            user, password, array_addresses = utils.get_array_connection_info_from_secret(request.secrets)

            unmap_volume(user, password, array_addresses, array_type, vol_id, initiators)

            logger.info("finished ControllerUnpublishVolume")
            return csi_pb2.ControllerUnpublishVolumeResponse()

        except controller_errors.VolumeAlreadyUnmappedError as ex:
            logger.debug("Idempotent case. volume is already unmapped.")
            return csi_pb2.ControllerUnpublishVolumeResponse()

        except controller_errors.PermissionDeniedError as ex:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(ex)
            return csi_pb2.ControllerPublishVolumeResponse()

        except (controller_errors.HostNotFoundError, controller_errors.VolumeNotFoundError) as ex:
            logger.exception(ex)
            context.set_details(ex.message)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return csi_pb2.ControllerUnpublishVolumeResponse()

        except Exception as ex:
            logger.debug("an internal exception occurred")
            logger.exception(ex)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('an internal exception occurred : {}'.format(ex))
            return csi_pb2.ControllerUnpublishVolumeResponse()

    def ValidateVolumeCapabilities(self, request, context):
        logger.info("ValidateVolumeCapabilities")
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        logger.info("finished ValidateVolumeCapabilities")
        return csi_pb2.ValidateVolumeCapabilitiesResponse()

    def ListVolumes(self, request, context):
        logger.info("ListVolumes")
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        logger.info("finished ListVolumes")
        return csi_pb2.ListVolumesResponse()

    def CreateSnapshot(self, request, context):
        # TODO
        set_current_thread_name(request.name)
        logger.info("Create snapshot : {}".format(snapshot_name))
        return csi_pb2.CreateSnapshotResponse()

    def DeleteSnapshot(self, request, context):
        # TODO
        logger.info("Delete snapshot")
        return csi_pb2.DeleteSnapshotResponse()

    def GetCapacity(self, request, context):
        logger.info("GetCapacity")
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        logger.info("finished GetCapacity")
        return csi_pb2.GetCapacityResponse()

    def ControllerGetCapabilities(self, request, context):
        logger.info("ControllerGetCapabilities")
        types = csi_pb2.ControllerServiceCapability.RPC.Type

        res = csi_pb2.ControllerGetCapabilitiesResponse(
            capabilities=[csi_pb2.ControllerServiceCapability(
                rpc=csi_pb2.ControllerServiceCapability.RPC(type=types.Value("CREATE_DELETE_VOLUME"))),
                csi_pb2.ControllerServiceCapability(
                    rpc=csi_pb2.ControllerServiceCapability.RPC(type=types.Value("CREATE_DELETE_SNAPSHOT"))),
                csi_pb2.ControllerServiceCapability(
                    rpc=csi_pb2.ControllerServiceCapability.RPC(type=types.Value("PUBLISH_UNPUBLISH_VOLUME")))])

        logger.info("finished ControllerGetCapabilities")
        return res

    def __get_identity_config(self, attribute_name):
        return self.cfg['identity'][attribute_name]

    def GetPluginInfo(self, request, context):
        logger.info("GetPluginInfo")
        try:
            name = self.__get_identity_config("name")
            version = self.__get_identity_config("version")
        except Exception as ex:
            logger.exception(ex)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('an error occured while trying to get plugin name or version')
            return csi_pb2.GetPluginInfoResponse()

        if not name or not version:
            logger.error("plugin name or version cannot be empty")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("plugin name or version cannot be empty")
            return csi_pb2.GetPluginInfoResponse()

        logger.info("finished GetPluginInfo")
        return csi_pb2.GetPluginInfoResponse(name=name, vendor_version=version)

    def GetPluginCapabilities(self, request, context):
        logger.info("GetPluginCapabilities")
        types = csi_pb2.PluginCapability.Service.Type
        capabilities = self.__get_identity_config("capabilities")
        capability_list = []
        for cap in capabilities:
            capability_list.append(
                csi_pb2.PluginCapability(
                    service=csi_pb2.PluginCapability.Service(type=types.Value(cap))
                )
            )

        logger.info("finished GetPluginCapabilities")
        return csi_pb2.GetPluginCapabilitiesResponse(
            capabilities=capability_list

        )

    def Probe(self, request, context):
        context.set_code(grpc.StatusCode.OK)
        return csi_pb2.ProbeResponse()

    def start_server(self):
        controller_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        csi_pb2_grpc.add_ControllerServicer_to_server(self, controller_server)
        csi_pb2_grpc.add_IdentityServicer_to_server(self, controller_server)

        # bind the server to the port defined above
        # controller_server.add_insecure_port('[::]:{}'.format(self.server_port))
        # controller_server.add_insecure_port('unix://{}'.format(self.server_port))
        controller_server.add_insecure_port(self.endpoint)

        # start the server
        logger.debug("Listening for connections on endpoint address: {}".format(self.endpoint))

        controller_server.start()
        logger.debug('Controller Server running ...')

        try:
            while True:
                time.sleep(60 * 60 * 60)
        except KeyboardInterrupt:
            controller_server.stop(0)
            logger.debug('Controller Server Stopped ...')


def main():
    parser = OptionParser()
    parser.add_option("-e", "--csi-endpoint", dest="endpoint",help="grpc endpoint")
    parser.add_option("-l", "--loglevel", dest="loglevel",help="log level")
    (options, args) = parser.parse_args()

    # set logger level and init logger
    log_level = options.loglevel
    set_log_level(log_level)

    # start the server
    endpoint = options.endpoint
    curr_server = ControllerServicer(endpoint)
    curr_server.start_server()


if __name__ == '__main__':
    main()
