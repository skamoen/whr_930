#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Interface with a StorkAir WHR930

Publish every 5 seconds the status on a MQTT topic
Listen on MQTT topic for commands to set the ventilation level
"""

import paho.mqtt.client as mqtt
import time
import sys
import serial
import logging

global debug
global debug_level
global warning
global mqttc
global ser
global pending_commands


def debug_data(serial_data):
    if debug is not True:
        return

    if debug_level > 0 and serial_data is not None:
        data_len = len(serial_data)
        if data_len == 2 and serial_data[0] == "07" and serial_data[1] == "f3":
            print(
                "Received an ack packet: {0} {1}".format(serial_data[0], serial_data[1])
            )
        else:
            print("Data length   : {0}".format(len(serial_data)))
            print("Ack           : {0} {1}".format(serial_data[0], serial_data[1]))
            print("Start         : {0} {1}".format(serial_data[2], serial_data[3]))
            print("Command       : {0} {1}".format(serial_data[4], serial_data[5]))
            print(
                "Nr data bytes : {0} (integer {1})".format(
                    serial_data[6], int(serial_data[6], 16)
                )
            )

            n = 1
            while n <= int(serial_data[6], 16):
                print(
                    "Data byte {0}   : Hex: {1}, Int: {2}, Array #: {3}".format(
                        n, serial_data[n + 6], int(serial_data[n + 6], 16), n + 6
                    )
                )
                n += 1

            print("Checksum      : {0}".format(serial_data[-2]))
            print("End           : {0} {1}".format(serial_data[-2], serial_data[-1]))

    if debug_level > 1:
        n = 0
        while n < len(serial_data):
            print("serial_data {0}   : {1}".format(n, serial_data[n]))
            n += 1


def publish_message(msg, mqtt_path):
    mqttc.publish(mqtt_path, payload=msg, qos=0, retain=True)
    time.sleep(0.1)
    logging.debug(
        "published message %s on topic %s",
        msg,
        mqtt_path
    )


def create_packet(command, data=None):
    """
    Create a packet.
    Data length and checksum are automatically calculated and added to the packet.
    Start and end bits are added as well.

    A packet is build up as follow:

        Start                : 2 bytes (0x07 0xF0)
        Command              : 2 bytes
        Number of data bytes : 1 byte
        Data bytes           : 0-n bytes
        Checksum             : 1 byte
        End                  : 2 bytes (0x07 0x0F)
    """
    if data is None:
        data = []
    packet = [0x07, 0xF0]

    for b in command:
        packet.append(b)

    packet.append(len(data))
    for b in data:
        packet.append(b)

    packet.append(calculate_checksum(packet[2:]))
    packet.append(0x07)  # default end bit
    packet.append(0x0F)  # default end bit

    return bytes(packet)


def calculate_checksum(data):
    """
    The checksum is obtained by adding all bytes (excluding start and end) plus 173.
    If the value 0x07 appears twice in the data area, only one 0x07 is used for the checksum calculation.
    If the checksum is larger than one byte, the least significant byte is used.
    """
    checksum = 173
    found_07 = False

    for b in data:
        if (b == 0x07 and found_07 is False) or b != 0x07:
            checksum += b

        if b == 0x07:
            found_07 = True

        if checksum > 0xFF:
            checksum -= 0xFF + 1

    return checksum


def calculate_incoming_checksum(data_raw):
    """The checksum over incoming data is calculated over the bytes starting from the default start bytes
    to the checksum value"""
    int_data = []
    for b in data_raw[4:-3]:
        int_data.append(int.from_bytes(b, "big"))
    return calculate_checksum(int_data)


def validate_data(data_raw):
    """Incoming data is in raw bytes. Convert to hex values for easier processing"""
    data = []
    for raw in data_raw:
        data.append(raw.hex())

    if len(data) <= 1:
        """always expect a valid ACK at least"""
        return None

    if len(data) == 2 and data[0] == "07" and data[1] == "f3":
        """
        This is a regular ACK which is received on all "setting" commands,
        such as setting ventilation level, command 0x99)
        """
        return data
    else:
        if len(data) >= 10:
            """If the data is more than a regular ACK, validate the checksum"""
            checksum = calculate_incoming_checksum(data_raw)
            if checksum != int.from_bytes(data_raw[-3], "big"):
                logging.warning(
                    "Checksum doesn't match (%s vs %s). Message ignored",
                    checksum,
                    int.from_bytes(data_raw[-3], "big"),
                )
                return None
            """
            A valid response should be at least 10 bytes (ACK + response with data length = 0)

            Byte 6 in the array contains the length of the dataset. This length + 10 is the total
            size of the message
            """
            dataset_len = int(data[6], 16)
            message_len = dataset_len + 10
            logging.debug("Message length is %s", message_len)

            """ 
            Sometimes more data is captured on the serial port then we expect. We drop those extra
            bytes to get a clean data to work on
            """
            stripped_data = data[0:message_len]
            logging.debug("Stripped message length is %s", len(stripped_data))

            if (
                stripped_data[0] != "07"
                or stripped_data[1] != "f3"
                or stripped_data[2] != "07"
                or stripped_data[3] != "f0"
                or stripped_data[-2] != "07"
                or stripped_data[-1] != "0f"
            ):
                logging.debug("Received garbage data, ignored ...")
                debug_data(stripped_data)
                return None
            else:
                logging.debug("Serial data validation passed")
                """
                Since we are here, we have a clean data set. Now we need to remove
                a double 0x07 in the dataset if present. This must be done because
                according the protocol specification, when a 0x07 value appears in
                the dataset, another 0x07 is inserted, but not added to the length
                or the checksum
                """
                try:
                    for i in range(7, 6 + dataset_len):
                        if stripped_data[i] == "07" and stripped_data[i + 1] == "07":
                            del stripped_data[i + 1]

                    return stripped_data
                except IndexError as _err:
                    """
                    The previous operation has thrown an IndexError which probably is
                    the result of a missing second '07'. We just issue a warning message
                    and return the stripped_data set
                    """
                    logging.warning(
                        "validate_data function got an IndexError, but we continued processing the data: %s",
                        _err,
                    )

        else:
            logging.warning(
                "The length of the data we received from the serial port is %s, it should be minimal 10 bytes",
                len(data),
            )
            return None


def serial_command(cmd):
    data = []
    ser.write(cmd)
    time.sleep(2)

    while ser.inWaiting() > 0:
        data.append(ser.read(1))

    return validate_data(data)


def status_8bit(inp):
    """
    Return the status of each bit in a 8 byte status
    """
    idx = 7
    matches = {}

    for num in (2 ** p for p in range(idx, -1, -1)):
        if ((inp - num) > 0) or ((inp - num) == 0):
            inp = inp - num
            matches[idx] = True
        else:
            matches[idx] = False

        idx -= 1

    return matches


def set_ventilation_level(fan_level):
    """
    Command: 0x00 0x99
    """
    if fan_level < 0 or fan_level > 3:
        logging.info(
            "Ventilation level can be set to 0, 1, 2 and 4, but not %s", fan_level
        )
        return None

    packet = create_packet([0x00, 0x99], [fan_level + 1])
    data = serial_command(packet)
    debug_data(data)

    if data:
        if data[0] == "07" and data[1] == "f3":
            logging.info("Changed the ventilation to %s", fan_level)
        else:
            logging.warning(
                "Changing the ventilation to %s went wrong, did not receive an ACK after the set command",
                fan_level,
            )
    else:
        logging.warning(
            "Changing the ventilation to %s went wrong, did not receive an ACK after the set command",
            fan_level,
        )


def set_comfort_temperature(temperature):
    """
    Command: 0x00 0xD3
    """
    calculated_temp = int(temperature + 20) * 2

    if temperature < 12 or temperature > 28:
        logging.debug(
            "Changing the comfort temperature to %s is outside the specification of the range min 12 and max 28",
            temperature,
        )
        return None

    packet = create_packet([0x00, 0xD3], [calculated_temp])
    data = serial_command(packet)
    debug_data(data)

    if data:
        if data[0] == "07" and data[1] == "f3":
            logging.info("Changed comfort temperature to %s", temperature)
        else:
            logging.warning(
                "Changing the comfort temperature to %s went wrong, did not receive an ACK after the set command",
                temperature,
            )
    else:
        logging.warning(
            "Changing the comfort temperature to %s went wrong, did not receive an ACK after the set command",
            temperature,
        )


def get_temp():
    """
    Command: 0x00 0xD1
    """
    packet = create_packet([0x00, 0xD1])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_temp function could not get serial data")
        else:
            """
            The default comfort temperature of the WHR930 is 20c

            Zehnder advises to let it on 20c, but if you want you change it, to
            set it to 21c in the winter and 15c in the summer.
            """
            comfort_temp = int(data[7], 16) / 2.0 - 20
            outside_air_temp = int(data[8], 16) / 2.0 - 20
            supply_air_temp = int(data[9], 16) / 2.0 - 20
            return_air_temp = int(data[10], 16) / 2.0 - 20
            exhaust_air_temp = int(data[11], 16) / 2.0 - 20

            publish_message(
                msg=comfort_temp, mqtt_path="house/2/attic/wtw/comfort_temp"
            )
            publish_message(
                msg=outside_air_temp, mqtt_path="house/2/attic/wtw/outside_air_temp"
            )
            publish_message(
                msg=supply_air_temp, mqtt_path="house/2/attic/wtw/supply_air_temp"
            )
            publish_message(
                msg=return_air_temp, mqtt_path="house/2/attic/wtw/return_air_temp"
            )
            publish_message(
                msg=exhaust_air_temp, mqtt_path="house/2/attic/wtw/exhaust_air_temp"
            )

            logging.debug(
                "comfort_temp: %s, outside_air_temp: %s, supply_air_temp: %s, return_air_temp: %s, exhaust_air_temp: %s",
                comfort_temp,
                outside_air_temp,
                supply_air_temp,
                return_air_temp,
                exhaust_air_temp,
            )
    except IndexError:
        logging.warning("get_temp ignoring incomplete message")


def get_ventilation_status():
    """
    Command: 0x00 0xCD
    """
    status_data = {"intake_fan_active": {0: False, 1: True}}

    packet = create_packet([0x00, 0xCD])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_ventilation_status function could not get serial data")
        else:
            return_air_level = int(data[13], 16)
            supply_air_level = int(data[14], 16)
            fan_level = int(data[15], 16) - 1
            intake_fan_active = status_data["intake_fan_active"][int(data[16], 16)]

            publish_message(
                msg=return_air_level, mqtt_path="house/2/attic/wtw/return_air_level"
            )
            publish_message(
                msg=supply_air_level, mqtt_path="house/2/attic/wtw/supply_air_level"
            )
            publish_message(
                msg=fan_level, mqtt_path="house/2/attic/wtw/ventilation_level"
            )
            publish_message(
                msg=intake_fan_active, mqtt_path="house/2/attic/wtw/intake_fan_active"
            )
            logging.debug(
                "return_air_level: %s, supply_air_level: %s, fan_level: %s, intake_fan_active: %s",
                return_air_level,
                supply_air_level,
                fan_level,
                intake_fan_active,
            )
    except IndexError:
        logging.warning("get_ventilation_status ignoring incomplete message")


def get_fan_status():
    """
    Command: 0x00 0x99
    """
    packet = create_packet([0x00, 0x0B])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_fan_status function could not get serial data")
        else:
            intake_fan_speed = int(data[7], 16)
            exhaust_fan_speed = int(data[8], 16)
            intake_fan_rpm = int(1875000 / (int(data[9], 16) * 256 + int(data[10], 16)))
            exhaust_fan_rpm = int(
                1875000 / (int(data[11], 16) * 256 + int(data[12], 16))
            )

            publish_message(
                msg=intake_fan_speed, mqtt_path="house/2/attic/wtw/intake_fan_speed"
            )
            publish_message(
                msg=exhaust_fan_speed, mqtt_path="house/2/attic/wtw/exhaust_fan_speed"
            )
            publish_message(
                msg=intake_fan_rpm, mqtt_path="house/2/attic/wtw/intake_fan_speed_rpm"
            )
            publish_message(
                msg=exhaust_fan_rpm, mqtt_path="house/2/attic/wtw/exhaust_fan_speed_rpm"
            )

            logging.debug(
                "intake_fan_speed %s, exhaust_fan_speed %s, intake_fan_rpm %s, exhaust_fan_rpm %s",
                intake_fan_speed,
                exhaust_fan_speed,
                intake_fan_rpm,
                exhaust_fan_rpm
            )
    except IndexError:
        logging.warning("get_fan_status ignoring incomplete message")


def get_filter_status():
    """
    Command: 0x00 0xD9
    """
    packet = create_packet([0x00, 0xD9])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_filter_status function could not get serial data")
        else:
            if int(data[15], 16) == 0:
                filter_status = "Ok"
            elif int(data[15], 16) == 1:
                filter_status = "Full"
            else:
                filter_status = "Unknown"

            publish_message(
                msg=filter_status, mqtt_path="house/2/attic/wtw/filter_status"
            )
            logging.debug("filter_status: %s", filter_status)
    except IndexError:
        logging.warning("get_filter_status ignoring incomplete message")


def get_valve_status():
    """
    Command: 0x00 0x0D
    """
    packet = create_packet([0x00, 0x0D])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_valve_status function could not get serial data")
        else:
            bypass = int(data[7], 16)
            """
            Status of the pre heating valve is exposed in get_preheating_status by the variable PreHeatingValveStatus
            PreHeating = int(data[8], 16)
            """
            bypass_motor_current = int(data[9], 16)
            pre_heating_motor_current = int(data[10], 16)

            publish_message(
                msg=bypass, mqtt_path="house/2/attic/wtw/valve_bypass_percentage"
            )
            publish_message(
                msg=bypass_motor_current,
                mqtt_path="house/2/attic/wtw/bypass_motor_current",
            )
            publish_message(
                msg=pre_heating_motor_current,
                mqtt_path="house/2/attic/wtw/preheating_motor_current",
            )

            logging.debug(
                "bypass: %s, bypass_motor_current: %s, pre_heating_motor_current: %s",
                bypass,
                bypass_motor_current,
                pre_heating_motor_current,
            )
    except IndexError:
        logging.warning("get_valve_status ignoring incomplete message")


def get_bypass_control():
    """
    Command: 0x00 0xDF
    """
    packet = create_packet([0x00, 0xDF])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_bypass_control function could not get serial data")
        else:
            bypass_factor = int(data[9], 16)
            bypass_step = int(data[10], 16)
            bypass_correction = int(data[11], 16)

            if int(data[13], 16) == 1:
                summer_mode = True
            else:
                summer_mode = False

            publish_message(
                msg=bypass_factor, mqtt_path="house/2/attic/wtw/bypass_factor"
            )
            publish_message(msg=bypass_step, mqtt_path="house/2/attic/wtw/bypass_step")
            publish_message(
                msg=bypass_correction, mqtt_path="house/2/attic/wtw/bypass_correction"
            )
            publish_message(msg=summer_mode, mqtt_path="house/2/attic/wtw/summer_mode")

            logging.debug(
                "bypass_factor: %s, bypass_step: %s, bypass_correction: %s, summer_mode: %s",
                bypass_factor,
                bypass_step,
                bypass_correction,
                summer_mode,
            )
    except IndexError:
        logging.warning("get_bypass_control ignoring incomplete message")


def get_preheating_status():
    """
    Command: 0x00 0xE1
    """
    status_data = {
        "pre_heating_valve_status": {0: "Closed", 1: "Open", "2": "Unknown"},
        "frost_protection_active": {0: False, 1: True},
        "pre_heating_active": {0: False, 1: True},
        "frost_protection_level": {
            0: "GuaranteedProtection",
            1: "HighProtection",
            2: "NominalProtection",
            3: "Economy",
        },
    }

    packet = create_packet([0x00, 0xE1])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_preheating_status function could not get serial data")
        else:
            pre_heating_valve_status = status_data["pre_heating_valve_status"][
                int(data[7], 16)
            ]
            frost_protection_active = status_data["frost_protection_active"][
                int(data[8], 16)
            ]
            pre_heating_active = status_data["pre_heating_active"][int(data[9], 16)]
            frost_protection_minutes = int(data[10], 16) + int(data[11], 16)
            frost_protection_level = status_data["frost_protection_level"][
                int(data[12], 16)
            ]

            publish_message(
                msg=pre_heating_valve_status,
                mqtt_path="house/2/attic/wtw/preheating_valve",
            )
            publish_message(
                msg=frost_protection_active,
                mqtt_path="house/2/attic/wtw/frost_protection_active",
            )
            publish_message(
                msg=pre_heating_active, mqtt_path="house/2/attic/wtw/preheating_state"
            )
            publish_message(
                msg=frost_protection_minutes,
                mqtt_path="house/2/attic/wtw/frost_protection_minutes",
            )
            publish_message(
                msg=frost_protection_level,
                mqtt_path="house/2/attic/wtw/frost_protection_level",
            )

            logging.debug(
                "pre_heating_valve_status: %s, frost_protection_active: %s, pre_heating_active: %s, "
                "frost_protection_minutes: %s, frost_protection_level: %s",
                pre_heating_valve_status,
                frost_protection_active,
                pre_heating_active,
                frost_protection_minutes,
                frost_protection_level,
            )
    except IndexError:
        logging.warning("get_preheating_status ignoring incomplete message")
    except KeyError as _err:
        logging.warning(
            "get_preheating_status incomplete message, missing a key: %s", _err
        )


def get_operating_hours():
    """
    Command: 0x00 0xDD
    """
    packet = create_packet([0x00, 0xDD])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_operating_hours function could not get serial data")
        else:
            level0_hours = int(data[7], 16) + int(data[8], 16) + int(data[9], 16)
            level1_hours = int(data[10], 16) + int(data[11], 16) + int(data[12], 16)
            level2_hours = int(data[13], 16) + int(data[14], 16) + int(data[15], 16)
            level3_hours = int(data[24], 16) + int(data[25], 16) + int(data[26], 16)
            frost_protection_hours = int(data[16], 16) + int(data[17], 16)
            pre_heating_hours = int(data[18], 16) + int(data[19], 16)
            bypass_open_hours = int(data[14], 16) + int(data[15], 16)
            filter_hours = int(data[22], 16) + int(data[23], 16)

            publish_message(
                msg=level0_hours, mqtt_path="house/2/attic/wtw/level0_hours"
            )
            publish_message(
                msg=level1_hours, mqtt_path="house/2/attic/wtw/level1_hours"
            )
            publish_message(
                msg=level2_hours, mqtt_path="house/2/attic/wtw/level2_hours"
            )
            publish_message(
                msg=level3_hours, mqtt_path="house/2/attic/wtw/level3_hours"
            )
            publish_message(
                msg=frost_protection_hours,
                mqtt_path="house/2/attic/wtw/frost_protection_hours",
            )
            publish_message(
                msg=pre_heating_hours, mqtt_path="house/2/attic/wtw/preheating_hours"
            )
            publish_message(
                msg=bypass_open_hours, mqtt_path="house/2/attic/wtw/bypass_open_hours"
            )
            publish_message(
                msg=filter_hours, mqtt_path="house/2/attic/wtw/filter_hours"
            )

            logging.debug(
                "level0_hours: %s, level1_hours: %s, level2_hours: %s, level3_hours: %s, "
                "frost_protection_hours: %s, pre_heating_hours: %s, bypass_open_hours: %s, filter_hours: %s",
                level0_hours,
                level1_hours,
                level2_hours,
                level3_hours,
                frost_protection_hours,
                pre_heating_hours,
                bypass_open_hours,
                filter_hours,
            )
    except IndexError:
        logging.warning("get_operating_hours ignoring incomplete message")


def get_status():
    """
    Command: 0x00 0xD5
    """
    status_data = {
        "preheating_present": {0: False, 1: True},
        "bypass_present": {0: False, 1: True},
        "type": {2: "Right", 1: "Left"},
        "size": {1: "Large", 2: "Small"},
        "options_present": {0: False, 1: True},
        "enthalpy_present": {0: False, 1: True, 2: "PresentWithoutSensor"},
        "ewt_present": {0: False, 1: "Managed", 2: "Unmanaged"},
    }

    active1_status_data = {
        0: "P10",
        1: "P11",
        2: "P12",
        3: "P13",
        4: "P14",
        5: "P15",
        6: "P16",
        7: "P17",
    }

    active2_status_data = {0: "P18", 1: "P19"}

    active3_status_data = {
        0: "P90",
        1: "P91",
        2: "P92",
        3: "P93",
        4: "P94",
        5: "P95",
        6: "P96",
        7: "P97",
    }

    packet = create_packet([0x00, 0xD5])
    data = serial_command(packet)
    debug_data(data)

    try:
        if data is None:
            logging.warning("get_status function could not get serial data")
        else:
            try:
                preheating_present = status_data["preheating_present"][int(data[7])]
                bypass_present = status_data["bypass_present"][int(data[8])]
                ventilator_type = status_data["type"][int(data[9])]
                size = status_data["size"][int(data[10])]
                options_present = status_data["options_present"][int(data[11])]
                active_status1 = int(data[13])  # (0x01 = P10 ... 0x80 = P17)
                active_status2 = int(data[14])  # (0x01 = P18 / 0x02 = P19)
                active_status3 = int(data[15])  # (0x01 = P90 ... 0x80 = P97)
                enthalpy_present = status_data["enthalpy_present"][int(data[16])]
                ewt_present = status_data["ewt_present"][int(data[17])]
            except ValueError as _value_err:
                logging.warning(
                    "get_status function received an inappropriate value: %s",
                    _value_err,
                )
                return
            except KeyError as _key_err:
                logging.warning(
                    "get status function missing key in dataset: %s, skipping message",
                    _key_err,
                )
                return

            logging.debug(
                "preheating_present: %s, bypass_present: %s, type: %s, size: %s, options_present: %s, "
                "enthalpy_present: %s, ewt_present: %s",
                preheating_present,
                bypass_present,
                ventilator_type,
                size,
                options_present,
                enthalpy_present,
                ewt_present,
            )

            for key, value in status_8bit(active_status1).items():
                topic = "house/2/attic/wtw/{0}_active".format(active1_status_data[key])
                logging.debug("%s: %s", topic, value)
                publish_message(msg=value, mqtt_path=topic)

            for key, value in status_8bit(active_status2).items():
                try:
                    topic = "house/2/attic/wtw/{0}_active".format(
                        active2_status_data[key]
                    )
                    logging.debug("%s: %s", topic, value)
                    publish_message(msg=value, mqtt_path=topic)
                except KeyError:
                    pass

            for key, value in status_8bit(active_status3).items():
                topic = "house/2/attic/wtw/{0}_active".format(active3_status_data[key])
                logging.debug("%s: %s", topic, value)
                publish_message(msg=value, mqtt_path=topic)

            publish_message(
                msg=preheating_present, mqtt_path="house/2/attic/wtw/preheating_present"
            )
            publish_message(
                msg=bypass_present, mqtt_path="house/2/attic/wtw/bypass_present"
            )
            publish_message(msg=ventilator_type, mqtt_path="house/2/attic/wtw/type")
            publish_message(msg=size, mqtt_path="house/2/attic/wtw/size")
            publish_message(
                msg=options_present, mqtt_path="house/2/attic/wtw/options_present"
            )
            publish_message(
                msg=enthalpy_present, mqtt_path="house/2/attic/wtw/enthalpy_present"
            )
            publish_message(msg=ewt_present, mqtt_path="house/2/attic/wtw/ewt_present")
    except IndexError:
        logging.warning("get_status ignoring incomplete message")


def on_message(client, userdata, message):
    logging.debug(
        "message received: topic: %s, payload: %s, userdata: %s",
        message.topic,
        message.payload,
        userdata,
    )

    pending_commands.append(message)


def handle_commands():

    while len(pending_commands) > 0:
        message = pending_commands.pop(0)
        if message.topic == "house/2/attic/wtw/set_ventilation_level":
            fan_level = int(float(message.payload))
            set_ventilation_level(fan_level)
            get_ventilation_status()
        elif message.topic == "house/2/attic/wtw/set_comfort_temperature":
            temperature = float(message.payload)
            set_comfort_temperature(temperature)
            get_temp()
        else:
            logging.info(
                "Received a message on topic %s where we do not have a handler for at the moment",
                message.topic,
            )


def recon():
    try:
        mqttc.reconnect()
        logging.info("Successful reconnected to the MQTT server")
        topic_subscribe()
    except:
        logging.warning(
            "Could not reconnect to the MQTT server. Trying again in 10 seconds"
        )
        time.sleep(10)
        recon()


def topic_subscribe():
    try:
        mqttc.subscribe(
            [
                ("house/2/attic/wtw/set_ventilation_level", 0),
                ("house/2/attic/wtw/set_comfort_temperature", 0),
            ]
        )
        logging.info("Successful subscribed to the MQTT topics")
    except:
        logging.warning(
            "There was an error while subscribing to the MQTT topic(s), trying again in 10 seconds"
        )
        time.sleep(10)
        topic_subscribe()


def on_connect(client, userdata, flags, rc):
    topic_subscribe()


def on_disconnect(client, userdata, rc):
    if rc != 0:
        logging.warning("Unexpected disconnection from MQTT, trying to reconnect")
        recon()


def main():
    global debug
    global debug_level
    global warning
    global mqttc
    global ser
    global pending_commands

    debug = False
    debug_level = 0
    warning = False

    log_level = logging.INFO
    if debug is True:
        log_level = logging.DEBUG

    logging.basicConfig(
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%d-%m-%Y %H:%M:%S",
        level=log_level,
    )

    pending_commands = []

    """Connect to the MQTT broker"""
    mqttc = mqtt.Client("whr930")
    mqttc.username_pw_set(username="myuser", password="mypass")

    """Define the mqtt callbacks"""
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.on_disconnect = on_disconnect

    """Connect to the MQTT server"""
    mqttc.connect("myhost/ip", port=1883, keepalive=45)

    """Open the serial port"""
    ser = serial.Serial(
        port="/dev/ttyUSB0",
        baudrate=9600,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
    )

    mqttc.loop_start()

    functions = [
        get_temp,
        get_ventilation_status,
        get_filter_status,
        get_fan_status,
        get_bypass_control,
        get_valve_status,
        get_status,
        get_operating_hours,
        get_preheating_status,
    ]

    while True:
        try:
            for func in functions:
                if len(pending_commands) == 0:
                    func()
                else:
                    handle_commands()

            time.sleep(5)
            pass
        except KeyboardInterrupt:
            mqttc.loop_stop()
            ser.close()
            break


if __name__ == "__main__":
    sys.exit(main())

"""End of program"""
