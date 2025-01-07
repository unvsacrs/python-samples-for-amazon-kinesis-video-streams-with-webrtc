import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib, GObject
import sys
import os
import argparse
import requests
import logging
from typing import Dict, Optional
from dotenv import load_dotenv

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

# Converts list of plugins to gst-launch string
def to_gst_string(elements):
    return " ! ".join(elements)

class GStreamerPipeline:
    def __init__(self, pipeline_str):
        # Initialize GStreamer
        Gst.init(None)

        # Create the pipeline
        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            print("Unable to create pipeline.")
            sys.exit(1)

        # Connect bus signals for message handling
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

    def run(self):
        # Set the pipeline to the playing state
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Unable to set the pipeline to the playing state")
            sys.exit(1)

        # Run the main loop
        self.loop = GLib.MainLoop()
        try:
            print("Pipeline is running. Press Ctrl+C to stop.")
            self.loop.run()
        except KeyboardInterrupt:
            print("Stopping pipeline...")
            self.close_pipeline()

        # Clean up after exiting the loop
        self.pipeline.set_state(Gst.State.NULL)

    def close_pipeline(self):
        # Retrieve webrtcbin element and close the NiceAgent
        webrtcbin = self.pipeline.get_by_name("ws")
        if webrtcbin:
            agent = webrtcbin.get_property("agent")
            if agent:
                print("Closing NiceAgent...")
                agent.close_async(self.on_agent_closed, None)
            else:
                print("No NiceAgent found.")
                self.cleanup()
        else:
            print("No webrtcbin found.")
            self.cleanup()

    def on_agent_closed(self, agent, user_data):
        print("NiceAgent closed successfully.")
        self.cleanup()

    def cleanup(self):
        # Clean up resources and set pipeline state to NULL
        print("Cleaning up resources...")
        self.pipeline.set_state(Gst.State.NULL)
        print("Resources cleaned up.")

    def on_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("End-of-stream received.")
            self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}: {debug}")
            self.loop.quit()
        elif t == Gst.MessageType.INFO:
            # Check for connection status messages from awskvswebrtcsink
            if message.get_structure() and message.get_structure().get_name() == "info":
                print("Connection established successfully!")

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

    def export_credentials_to_env(credentials: Dict[str, str]) -> None:
        """Export credentials to environment variables."""
        try:
            # Set environment variables
            os.environ['AWS_ACCESS_KEY_ID'] = credentials['accessKeyId']
            os.environ['AWS_SECRET_ACCESS_KEY'] = credentials['secretAccessKey']

            if 'sessionToken' in credentials:
                os.environ['AWS_SESSION_TOKEN'] = credentials['sessionToken']

            print("AWS credentials successfully exported to environment variables")

        except Exception as e:
            print(f"Error exporting credentials to environment variables: {str(e)}")
            raise


def main():
    ###############################
    # make sure to change channel-name to yours
    ###############################
    DEFAULT_PIPELINE = to_gst_string([
        "autovideosrc",
        "video/x-raw,width=1280,height=720",
        "videoconvert",
        "textoverlay text=\"Hello from GStreamer\"",
        "videoconvert",
        "awskvswebrtcsink name=ws signaller::channel-name=kvs-demo-channel"
    ])

    parser = argparse.ArgumentParser(description='Kinesis Video Streams WebRTC Client')
    parser.add_argument("--pipeline", default=DEFAULT_PIPELINE, help="Gstreamer pipeline without gst-launch")
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
        # Export AWS access key as environment variables
        IoTCredentialProvider.export_credentials_to_env(credentials)

    # Create and run the GStreamer pipeline
    pipeline = GStreamerPipeline(args.pipeline)
    pipeline.run()

if __name__ == "__main__":
    main()
