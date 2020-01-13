import argparse
import datetime
import logging
import threading
import time
from pprint import pprint

import pychromecast
import toml
from broadlink import sp2
from pychromecast.controllers.media import MediaController


TIMEOUT = 60

OFF = False
ON = True


class SpeakerConnectError(Exception):
    pass


def power_state_as_text(state):
    return "on" if state else "off"


class SpeakerAccess(object):
    def __init__(self, name, host, port, mac, devtype):
        self.name = name
        self.host = host
        self.port = port
        self.mac = mac
        self.devtype = devtype

        self._sp = None

        # Connect first time to authenticate
        try:
            self.connect()
        except SpeakerConnectError as e:
            logging.warning("Could not connect to %s speaker: %s", self.name, e)

    def connect(self):
        try:
            self._sp = sp2((self.host, self.port), self.mac, self.devtype)
            self._sp.auth()
        except Exception as e:
            raise SpeakerConnectError("Connection error")

        logging.info("Connected to %s speaker", self.name)

    @property
    def sp(self):
        if not self._sp:
            self.connect()

        return self._sp

    def set_state(self, state):
        try:
            self.pvt_set_power_state(state)
        except SpeakerConnectError as e:
            logging.warning(
                "Could not turn %s %s speaker: %s",
                power_state_as_text(state),
                self.name,
                e,
            )

    def pvt_set_power_state(self, state):
        try:
            if self.sp.check_power() != state:
                logging.info(
                    "Turning %s %s speaker" % (power_state_as_text(state), self.name)
                )
                self.sp.set_power(state)
        except IOError:
            self._sp = None
            raise SpeakerConnectError("Communication error")

    @classmethod
    def from_config(cls, name, config):
        mac = bytearray.fromhex(config["mac"])
        host, port = config["address"].split(":")

        return cls(name, host, int(port), mac, config["devtype"])


class SpeakerRemote:
    def __init__(self, config):
        self.speakers = [
            SpeakerAccess.from_config(name, speaker_conf)
            for name, speaker_conf in config.items()
        ]

    def switch_on(self):
        self.set_power_state(True)

    def switch_off(self):
        self.set_power_state(False)

    def set_power_state(self, state):
        for speaker in self.speakers:
            threading.Thread(target=speaker.set_state, args=(state,)).start()


class SpeakerThread(threading.Thread):
    PLAYING = 1
    STOPPED = 2
    INACTIVE = 3

    def __init__(self, remote):
        super(SpeakerThread, self).__init__()
        self.setDaemon(True)

        self.remote = remote
        self._state = 0
        self._state_changed = None
        self._event = threading.Event()

    def run(self):
        while True:
            self._event.wait(timeout=10)

            if self._event.is_set():
                self._event.clear()

            if self._state == self.PLAYING:
                self.remote.switch_on()
            elif self._state == self.INACTIVE:
                self.remote.switch_off()
            else:
                if self.state_changed_seconds_ago() > TIMEOUT:
                    self.remote.switch_off()

    def signal_playing(self):
        self.set_state(self.PLAYING)

    def signal_stopped(self):
        self.set_state(self.STOPPED)

    def signal_inactive(self):
        self.set_state(self.INACTIVE)

    def set_state(self, state):
        if self._state != state:
            self._state = state
            self._state_changed = datetime.datetime.utcnow()
            self._event.set()

            if state == self.PLAYING:
                logging.info("Chromecast state changed to PLAYING")
            elif state == self.STOPPED:
                logging.info("Chromecast state changed to STOPPED")
            else:
                logging.info("Chromecast state changed to INACTIVE")

    def state_changed_seconds_ago(self):
        if self._state_changed is None:
            return TIMEOUT + 1
        return (datetime.datetime.utcnow() - self._state_changed).seconds


class MyController(MediaController):
    def __init__(self, speaker_thread):
        super(MyController, self).__init__()
        self.speaker_thread = speaker_thread

    def signal_speakers(self):
        if not self.is_active:
            self.speaker_thread.signal_inactive()
        elif self.is_playing:
            self.speaker_thread.signal_playing()
        else:
            self.speaker_thread.signal_stopped()

    def new_media_status(self, status):
        self.signal_speakers()

    def new_cast_status(self, status):
        self.signal_speakers()

    def new_connection_status(self, status):
        self.signal_speakers()


def main(config_file):
    cast = None

    with open(config_file, "r") as fp:
        config = toml.load(fp)

    cast_name = config["chromecast"]["name"]

    remote = SpeakerRemote(config["speakers"])

    speaker_thread = SpeakerThread(remote)
    speaker_thread.start()

    while cast is None:
        logging.info("Connecting to %s", cast_name)
        try:
            cast = next(
                cc
                for cc in pychromecast.get_chromecasts()
                if cc.device.friendly_name == cast_name
            )
        except StopIteration:
            pass

        logging.warning("Could not connect to chromecast, retrying in 10 seconds")
        time.sleep(2)

    logging.info("Connected to %s", cast_name)

    controller = MyController(speaker_thread)
    controller.register_status_listener(controller)

    cast.register_handler(controller)
    cast.register_connection_listener(controller)
    cast.register_status_listener(controller)
    cast.wait()

    controller.signal_speakers()


    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Stopping")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The MFB remote daemon")
    parser.add_argument(
        "config", metavar="CONFIG_FILE", help="the .toml configuration file"
    )
    parser.add_argument(
        "-v",
        dest="log_level",
        help="enable verbose logging (debug level)",
        action="store_const",
        const="DEBUG",
        default="INFO",
    )

    options = parser.parse_args()

    logging.basicConfig(level=options.log_level)
    logging.debug("Verbose mode enabled")

    main(options.config)
