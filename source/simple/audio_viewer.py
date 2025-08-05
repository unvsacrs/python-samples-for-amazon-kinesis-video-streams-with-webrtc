import argparse
import asyncio
import boto3
import json
import platform
import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay, MediaRecorder
from aiortc.sdp import candidate_from_sdp
from base64 import b64decode, b64encode
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import Session
import threading
import queue
import os
import sys
import time
import logging
import requests
from typing import Dict, Optional
from dotenv import load_dotenv
from get_sts import get_session_token_basic
import pyaudio
import numpy as np
from av import AudioFrame

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# Construct script_output_path
script_output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../infra/script-output')

load_dotenv(dotenv_path=f"{script_output_path}/.env")  # take environment variables from .env.
IOT_CREDENTIAL_PROVIDER = os.getenv('IOT_CREDENTIAL_PROVIDER')
THING_NAME = os.getenv('THING_NAME')
ROLE_ALIAS = os.getenv('ROLE_ALIAS')
CERT_FILE = f"{script_output_path}/{os.getenv('CERT_FILE')}"
KEY_FILE = f"{script_output_path}/{os.getenv('KEY_FILE')}"
ROOT_CA = f"{script_output_path}/{os.getenv('ROOT_CA')}"
AWS_DEFAULT_REGION = os.getenv('AWS_DEFAULT_REGION')


class AudioStreamHandler:
    def __init__(self):
        self.audio_queue = queue.Queue(maxsize=50)  # Limit queue size to prevent latency
        self.con_flag = False
        self.end_flag = False
        self.initialized = False
        
        # PyAudio setup
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.sample_rate = 48000  # Default sample rate, will be updated based on received audio
        self.channels = 2  # Default channels, will be updated based on received audio
        
    def start_audio_playback(self):
        """Start audio playback stream"""
        if self.stream is None:
            # Try different buffer sizes for better performance
            frames_per_buffer = 2048  # Increased from 1024
            
            self.stream = self.p.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                frames_per_buffer=frames_per_buffer,
                stream_callback=None  # Use blocking mode
            )
            logging.info(f"Audio playback started: {self.sample_rate}Hz, {self.channels} channels, buffer={frames_per_buffer}")
    
    def stop_audio_playback(self):
        """Stop audio playback stream"""
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        if self.p:
            self.p.terminate()
        
    def play_audio(self):
        """Main audio playback loop"""
        logging.info("Audio playback thread started")
        buffer_count = 0
        
        while not self.end_flag:
            try:
                if not self.audio_queue.empty():
                    audio_data = self.audio_queue.get(timeout=0.1)
                    buffer_count += 1
                    
                    if self.stream and self.stream.is_active():
                        # Ensure data is contiguous and in the right format
                        audio_bytes = np.ascontiguousarray(audio_data, dtype=np.float32).tobytes()
                        self.stream.write(audio_bytes)
                        
                        # Log buffer info for first few buffers
                        if buffer_count <= 5:
                            logging.info(f"Buffer {buffer_count}: shape={audio_data.shape}, "
                                       f"min={audio_data.min():.3f}, max={audio_data.max():.3f}")
                else:
                    time.sleep(0.001)  # Small sleep to prevent busy waiting
            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"Error playing audio: {e}")
                import traceback
                traceback.print_exc()
                
        self.stop_audio_playback()
        logging.info("Audio playback thread ended")


# Remove the SimpleAudioTrack class completely as it's no longer needed

class MediaTrackManager:
    def __init__(self, file_path=None):
        self.file_path = file_path

    def create_media_track(self):
        # For audio-only viewer, we don't need to create local media tracks
        # Just return None for both tracks
        return None, None


class KinesisVideoClient:
    def __init__(self, client_id, region, channel_arn, credentials, audio_handler, file_path=None):
        self.client_id = client_id
        self.region = region
        self.channel_arn = channel_arn
        self.credentials = credentials
        self.media_manager = MediaTrackManager(file_path)
        if self.credentials:
            self.kinesisvideo = boto3.client('kinesisvideo',
                                             region_name=self.region,
                                             aws_access_key_id=self.credentials['accessKeyId'],
                                             aws_secret_access_key=self.credentials['secretAccessKey'],
                                             aws_session_token=self.credentials['sessionToken']
                                             )
        else:
            self.kinesisvideo = boto3.client('kinesisvideo', region_name=self.region)
        self.endpoints = None
        self.endpoint_https = None
        self.endpoint_wss = None
        self.ice_servers = None
        self.audio_handler = audio_handler
        self.pc = None

    def get_signaling_channel_endpoint(self):
        if self.endpoints is None:  # Check if endpoints are already fetched
            endpoints = self.kinesisvideo.get_signaling_channel_endpoint(
                ChannelARN=self.channel_arn,
                SingleMasterChannelEndpointConfiguration={'Protocols': ['HTTPS', 'WSS'], 'Role': 'VIEWER'}
            )
            self.endpoints = {
                'HTTPS': next(o['ResourceEndpoint'] for o in endpoints['ResourceEndpointList'] if o['Protocol'] == 'HTTPS'),
                'WSS': next(o['ResourceEndpoint'] for o in endpoints['ResourceEndpointList'] if o['Protocol'] == 'WSS')
            }
            self.endpoint_https = self.endpoints['HTTPS']
            self.endpoint_wss = self.endpoints['WSS']
        return self.endpoints

    def prepare_ice_servers(self):
        if self.credentials:
            kinesis_video_signaling = boto3.client('kinesis-video-signaling',
                                                   endpoint_url=self.endpoint_https,
                                                   region_name=self.region,
                                                   aws_access_key_id=self.credentials['accessKeyId'],
                                                   aws_secret_access_key=self.credentials['secretAccessKey'],
                                                   aws_session_token=self.credentials['sessionToken']
                                                   )
        else:
            kinesis_video_signaling = boto3.client('kinesis-video-signaling',
                                                   endpoint_url=self.endpoint_https,
                                                   region_name=self.region)
        ice_server_config = kinesis_video_signaling.get_ice_server_config(
            ChannelARN=self.channel_arn,
            ClientId=self.client_id
        )

        iceServers = [RTCIceServer(urls=f'stun:stun.kinesisvideo.{self.region}.amazonaws.com:443')]
        for iceServer in ice_server_config['IceServerList']:
            iceServers.append(RTCIceServer(
                urls=iceServer['Uris'],
                username=iceServer['Username'],
                credential=iceServer['Password']
            ))
        self.ice_servers = iceServers

        return self.ice_servers

    def create_wss_url(self):
        if self.credentials:
            auth_credentials = Credentials(
                access_key=self.credentials['accessKeyId'],
                secret_key=self.credentials['secretAccessKey'],
                token=self.credentials['sessionToken']
            )
        else:
            session = Session()
            auth_credentials = session.get_credentials()

        SigV4 = SigV4QueryAuth(auth_credentials, 'kinesisvideo', self.region, 299)
        aws_request = AWSRequest(
            method='GET',
            url=self.endpoint_wss,
            params={'X-Amz-ChannelARN': self.channel_arn, 'X-Amz-ClientId': self.client_id},
        )
        SigV4.add_auth(aws_request)
        PreparedRequest = aws_request.prepare()
        return PreparedRequest.url

    def decode_msg(self, msg):
        try:
            data = json.loads(msg)
            payload = json.loads(b64decode(data['messagePayload'].encode('ascii')).decode('ascii'))
            return data['messageType'], payload, data.get('senderClientId')
        except json.decoder.JSONDecodeError:
            return '', {}, ''

    def encode_msg(self, action, payload, client_id):
        return json.dumps({
            'action': action,
            'messagePayload': b64encode(json.dumps(payload).encode('ascii')).decode('ascii'),
            'recipientClientId': client_id,
        })

    async def handle_sdp_offer(self, websocket):
        iceServers = self.prepare_ice_servers()
        configuration = RTCConfiguration(iceServers=iceServers)
        self.pc = RTCPeerConnection(configuration=configuration)

        @self.pc.on('connectionstatechange')
        async def on_connectionstatechange():
            logging.info(f'[{self.client_id}] connectionState: {self.pc.connectionState}')
            self.audio_handler.con_flag = self.pc.connectionState == "connected"

        @self.pc.on('iceconnectionstatechange')
        async def on_iceconnectionstatechange():
            logging.info(f'[{self.client_id}] ICE connectionState: {self.pc.iceConnectionState}')

        @self.pc.on('icegatheringstatechange')
        async def on_icegatheringstatechange():
            logging.info(f'[{self.client_id}] ICE gatheringState: {self.pc.iceGatheringState}')

        @self.pc.on('track')
        def on_track(track):
            logging.info(f"Received track: {track.kind}")
            if track.kind == "audio":
                # Start processing the received audio track
                asyncio.ensure_future(self.process_audio_track(track))

        @self.pc.on('icecandidate')
        async def on_icecandidate(event):
            if event.candidate:
                logging.info(f"Local ICE candidate: {event.candidate}")
                await websocket.send(self.encode_msg('ICE_CANDIDATE', {
                    'candidate': event.candidate.candidate,
                    'sdpMid': event.candidate.sdpMid,
                    'sdpMLineIndex': event.candidate.sdpMLineIndex,
                }, self.client_id))

        # Add audio transceiver for receiving audio
        self.pc.addTransceiver('audio', direction='recvonly')
        
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        await websocket.send(self.encode_msg('SDP_OFFER', {'sdp': self.pc.localDescription.sdp, 'type': self.pc.localDescription.type}, self.client_id))

    async def process_audio_track(self, track):
        """Process incoming audio track and send to audio handler"""
        logging.info("Starting audio track processing")
        frame_count = 0
        
        while True:
            try:
                frame = await track.recv()
                frame_count += 1
                
                # Get audio parameters from first frame
                if not self.audio_handler.initialized:
                    self.audio_handler.sample_rate = frame.sample_rate
                    self.audio_handler.channels = len(frame.layout.channels)
                    self.audio_handler.start_audio_playback()
                    self.audio_handler.initialized = True
                    logging.info(f"Audio initialized: {frame.sample_rate}Hz, {len(frame.layout.channels)} channels")
                    logging.info(f"Frame format: {frame.format.name}, layout: {frame.layout.name}")
                
                # Convert to numpy array - get raw samples
                audio_data = frame.to_ndarray()
                
                # Debug logging for first few frames
                if frame_count <= 5:
                    logging.info(f"Frame {frame_count}: shape={audio_data.shape}, dtype={audio_data.dtype}, "
                               f"min={audio_data.min():.3f}, max={audio_data.max():.3f}")
                
                # Handle different audio formats
                if frame.format.name == 's16':
                    # Convert from int16 to float32 in range [-1, 1]
                    audio_data = audio_data.astype(np.float32) / 32768.0
                elif frame.format.name == 's32':
                    # Convert from int32 to float32 in range [-1, 1]
                    audio_data = audio_data.astype(np.float32) / 2147483648.0
                elif frame.format.name == 'flt':
                    # Already float32, just ensure type
                    audio_data = audio_data.astype(np.float32)
                elif frame.format.name == 'fltp':
                    # Planar float format
                    audio_data = audio_data.astype(np.float32)
                
                # Ensure audio is in range [-1, 1]
                audio_data = np.clip(audio_data, -1.0, 1.0)
                
                # Handle channel layout
                if audio_data.ndim == 2:
                    # Check if we need to transpose (channels, samples) -> (samples, channels)
                    if audio_data.shape[0] <= 2 and audio_data.shape[1] > 2:
                        audio_data = audio_data.T
                elif audio_data.ndim == 1:
                    # Mono audio
                    if self.audio_handler.channels == 2:
                        # Convert mono to stereo by duplicating
                        audio_data = np.column_stack([audio_data, audio_data])
                    else:
                        # Keep as mono, but ensure it's the right shape
                        audio_data = audio_data.reshape(-1, 1)
                
                # Add to playback queue
                self.audio_handler.audio_queue.put(audio_data)
                
            except Exception as e:
                logging.error(f"Error processing audio track: {e}")
                import traceback
                traceback.print_exc()
                break

    async def handle_ice_candidate(self, payload):
        candidate = candidate_from_sdp(payload['candidate'])
        candidate.sdpMid = payload['sdpMid']
        candidate.sdpMLineIndex = payload['sdpMLineIndex']
        logging.info(f"Adding remote ICE candidate: {candidate}")
        await self.pc.addIceCandidate(candidate)

    async def signaling_client(self):
        self.get_signaling_channel_endpoint()
        wss_url = self.create_wss_url()
        try:
            async with websockets.connect(wss_url) as websocket:
                logging.info('Signaling Server Connected!')
                await self.handle_sdp_offer(websocket)
                await self.handle_messages(websocket)
        except Exception as e:
            logging.error(f"Exception: {e}")
        finally:
            self.audio_handler.con_flag = False
            if self.pc and self.pc.connectionState != "closed":
                await self.pc.close()

    async def handle_messages(self, websocket):
        async for message in websocket:
            msg_type, payload, _ = self.decode_msg(message)
            if msg_type == 'SDP_ANSWER':
                logging.info(f"Received SDP answer: {payload}")
                await self.pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type=payload["type"]))
            elif msg_type == 'ICE_CANDIDATE':
                try:
                    await self.handle_ice_candidate(payload)
                except Exception as e:
                    logging.error(f"Error adding ICE candidate: {e}")

        while True:
            await asyncio.sleep(1)
            if self.pc.iceConnectionState == "failed":
                logging.error("ICE connection failed")
                break


class IoTCredentialProvider:
    def __init__(self, endpoint: str, region: str, thing_name: str, role_alias: str,
                 cert_path: str, key_path: str, root_ca_path: str):
        self.endpoint = endpoint
        self.region = region
        self.thing_name = thing_name
        self.role_alias = role_alias
        self.cert_path = cert_path
        self.key_path = key_path
        self.root_ca_path = root_ca_path

    def get_temporary_credentials(self) -> Optional[Dict[str, str]]:
        url = f"https://{self.endpoint}/role-aliases/{self.role_alias}/credentials"
        headers = {'x-amzn-iot-thingname': self.thing_name}

        try:
            response = requests.get(
                url,
                headers=headers,
                cert=(self.cert_path, self.key_path),
                verify=self.root_ca_path,
                timeout=(10, 20)  # 10 seconds for connecting, 20 seconds for reading
            )

            if response.status_code == 200:
                credentials = response.json()['credentials']
                print("Temporary credentials obtained successfully.")
                return credentials
            else:
                print(f"Failed to obtain credentials. Status code: {response.status_code}")
                print(f"Response: {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"An error occurred: {e}")
            return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel-arn', type=str, required=True, help='the ARN of the signaling channel')
    parser.add_argument('--use-device-certs', action='store_true', help='Use system certificates')
    args = parser.parse_args()

    if not AWS_DEFAULT_REGION:
        raise Exception("AWS_DEFAULT_REGION environment variable should be configured.\ni.e. export AWS_DEFAULT_REGION=us-west-2")

    if args.use_device_certs:
        provider = IoTCredentialProvider(
            endpoint=IOT_CREDENTIAL_PROVIDER,
            region=AWS_DEFAULT_REGION,
            thing_name=THING_NAME,
            role_alias=ROLE_ALIAS,
            cert_path=CERT_FILE,
            key_path=KEY_FILE,
            root_ca_path=ROOT_CA
        )
        credentials = provider.get_temporary_credentials()
        if not credentials:
            raise Exception("Failed to obtain temporary credentials")
    else:
        token = get_session_token_basic()

        credentials = {}
        credentials['accessKeyId'] = token['AccessKeyId']
        credentials['secretAccessKey'] = token['SecretAccessKey']
        credentials['sessionToken'] = token['SessionToken']

    audio_handler = AudioStreamHandler()

    # Start the audio playback thread
    audio_thread = threading.Thread(target=audio_handler.play_audio)
    audio_thread.start()

    # Start the WebRTC connection
    try:
        asyncio.run(
            KinesisVideoClient(
                client_id="AUDIO_VIEWER",
                region=AWS_DEFAULT_REGION,
                channel_arn=args.channel_arn,
                credentials=credentials,
                audio_handler=audio_handler
            ).signaling_client())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        audio_handler.end_flag = True
        audio_thread.join()
        logging.info("Application ended")

if __name__ == '__main__':
    main()