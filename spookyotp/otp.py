from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
import base64
from os import urandom
import qrcode
try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote
import time
import hmac
import hashlib
from spookyotp.byte_util import (int_to_bytearray,
                                 bytes_to_31_bit_int)


def get_random_secret(n_bytes=10):
    """
    Return a new, n-byte (default: 10) random secret
    """
    return bytearray(urandom(n_bytes))


def constant_time_compare(str_a, str_b):
    """
    Compare two strings, taking constant time
    """
    are_equal = len(str_a) == len(str_b)
    for a, b in zip(str_a, str_b):
        are_equal &= (a == b)
    return are_equal


class OTPBase(object):
    """
    Base class for the OTP generators
    """
    _otp_type = 'otp'
    _extra_uri_parameters = frozenset()

    def __init__(self):
        raise NotImplementedError()

    def _setup(self, secret, issuer, account,
               n_digits, algorithm):
        """
        Store the secret and other parameters needed
        to generate OTP codes
        """
        if isinstance(secret, bytearray):
            self._secret = secret
        else:
            self._secret = bytearray(base64.b32decode(secret))
        self._issuer = issuer
        self._account = account
        self._n_digits = int(n_digits)
        self._algorithm_name = algorithm.lower()
        self._algorithm = self._get_algorithm(self._algorithm_name)

    @staticmethod
    def _get_algorithm(algorithm_name):
        """
        Try to load the named algorithm for use during hashing.
        """
        try:
            return getattr(hashlib, algorithm_name)
        except AttributeError:
            raise ValueError("Not a valid algorithm: '{}'"
                             .format(algorithm_name))

    def get_qr_code(self):
        """
        Return a QR Code (as generated by the qrcode package)
        that can be used to load the parameters onto a phone etc.
        """
        uri = self.get_uri()
        img = qrcode.make(uri)
        return img

    def save_qr_code(self, filename):
        """
        Save a QR Code to the supplied filename. Type is inferred
        from the filename.
        """
        qr_code = self.get_qr_code()
        qr_code.save(filename)

    @classmethod
    def _get_uri(cls, secret, issuer, account=None,
                 n_digits=None, algorithm=None, **other_params):
        """
        Return a URL that encodes the OTP parameters so they can
        be loaded onto a phone (or the like) via a QR code.

        Complies with the google-authenticator KeyUriFormat
        """
        encoded_otp_type = quote(cls._otp_type)
        encoded_secret = base64.b32encode(secret).decode()
        encoded_issuer = quote(issuer)
        if account is not None:
            encoded_path = '{}:{}'.format(encoded_issuer, quote(account))
        else:
            encoded_path = encoded_issuer
        encoded_algorithm = quote(algorithm)

        uri = ("otpauth://{0}/{1}?secret={2}&issuer={3}"
               "&digits={4}&algorithm={5}"
               .format(encoded_otp_type, encoded_path, encoded_secret,
                       encoded_issuer, n_digits, encoded_algorithm))
        for key, value in other_params.items():
            if key not in cls._extra_uri_parameters:
                raise ValueError("Got unexpected URL keyword '{}'"
                                 .format(key))
            uri += '&{0}={1}'.format(key, quote(str(value)))
        return uri

    @staticmethod
    def _get_otp(secret, counter_int, n_digits, algorithm):
        """
        Apply the HOTP algorithm from RFC 4226 to generate a
        one-time code string.
        """
        if counter_int.bit_length() > 64:
            raise ValueError("Counter must fit in a unsigned, 64-bit integer")
        counter = int_to_bytearray(counter_int)
        hashed = bytearray(hmac.new(secret, counter, algorithm).digest())
        idx = hashed[-1] & 0x0f
        truncated = hashed[idx:idx + 4]
        as_int = bytes_to_31_bit_int(truncated)
        pad_str = '{:0' + str(n_digits) + '}'
        to_display = pad_str.format(as_int)[-n_digits:]
        return to_display

    @staticmethod
    def _compare(code_a, code_b):
        """
        Compare two one-time codes to each other. Returns True if they match.
        """
        for code in (code_a, code_b):
            try:
                int(code, 10)
            except ValueError:
                raise ValueError("'{}' is not a valid OTP code".format(code))
        return constant_time_compare(code_a, code_b)


class TOTP(OTPBase):
    _otp_type = 'totp'
    _extra_uri_parameters = frozenset(['period'])

    def __init__(self, secret, issuer, account=None,
                 n_digits=6, algorithm='sha1', period=30,
                 time_source=None):
        """
        Generates TOTP (time-based) codes.

        Args:
          secret (bytearray or str): The shared secret used to generate
                                     codes. If str, must be base32 encoded.
          issuer (str): The issuer who provides or manages the account
          account (str): A label for the account that uses the OTP
          n_digits (int, optional): The number of digits each code
                                    uses (default: 6)
          algorithm (str, optional): The hashing algorithm to use when
                                     generating the OTP code (default: 'sha1')
          period (int, optional): How long each code is valid for, in seconds
                                  (default: 30)
          time_source(function, optional): A function that returns an integer
                                           timestamp (default: time.time)
        """
        self._setup(secret, issuer, account,
                    n_digits, algorithm)
        self._period = int(period)
        self._current_timestamp = time_source or time.time

    def get_uri(self):
        """
        Return a URL that encodes the OTP parameters so they can
        be loaded onto a phone (or the like) via a QR code.

        Complies with the google-authenticator KeyUriFormat
        """
        return self._get_uri(self._secret, self._issuer,
                             self._account, self._n_digits,
                             self._algorithm_name, period=self._period)

    def get_otp(self, timestamp=None):
        """
        Get the TOTP for a specified time, or now by default.

        Args:
          timestamp (int or float, optional): The timestamp to get a code for
                                              in seconds since an epoch.
                                              (default: now)
        """
        if timestamp is None:
            timestamp = self._current_timestamp()
        otp = self._get_otp(self._secret,
                            int(timestamp)//self._period,
                            self._n_digits, self._algorithm)
        return otp

    def compare(self, code, max_step_difference=1):
        """
        Check the code to see if it's valid, by default looking
        at the current, most recent past, and next future code
        to allow for clock skew.
        Returns True if the code is valid.

        Args:
          code (str): The code to check
          max_step_difference (int, optional): Check +/- this many valid
                                               codes around the current one
                                               to allow for clock skew.
                                               (default: 1)
        """
        if max_step_difference < 0:
            raise ValueError("Max step difference must be non-negative")
        timestamp = self._current_timestamp()
        valid_codes = [self.get_otp(timestamp + i * self._period)
                       for i in range(-max_step_difference,
                                      max_step_difference + 1)]
        return any([self._compare(code, valid) for valid in valid_codes])


class HOTP(OTPBase):
    _otp_type = 'hotp'
    _extra_uri_parameters = frozenset(['counter'])

    def __init__(self, secret, issuer, account=None,
                 n_digits=6, algorithm='sha1', counter=0):
        """
        Generates HOTP (incrementing counter-based) codes.

        Args:
          secret (bytearray or str): The shared secret used to generate
                                     codes. If str, must be base32 encoded.
          issuer (str): The issuer who provides or manages the account
          account (str): A label for the account that uses the OTP
          n_digits (int, optional): The number of digits each code
                                    uses (default: 6)
          algorithm (str, optional): The hashing algorithm to use when
                                     generating the OTP code (default: 'sha1')
          counter (int, optional): The initial counter value (default: 0)
        """
        self._setup(secret, issuer, account,
                    n_digits, algorithm)
        self.counter = int(counter)

    def get_uri(self):
        """
        Return a URL that encodes the OTP parameters so they can
        be loaded onto a phone (or the like) via a QR code.

        Complies with the google-authenticator KeyUriFormat
        """
        return self._get_uri(self._secret, self._issuer,
                             self._account, self._n_digits,
                             self._algorithm_name, counter=self.counter)

    def get_otp(self, counter=None, auto_increment=True):
        """
        Get the HOTP for a specified counter, or the current one by default.
        If no counter is specified, and auto_increment is True (default)
        the counter will automatically increment for generating the next code.

        Args:
          counter (int, optional): The counter value (default: current value)
          auto_increment (bool, optional): Automatically increment the counter
                                           if one wasn't specified. Does
                                           nothing if the counter was
                                           specified. (default: True)
        """
        if counter is None:
            counter = self.counter
            if auto_increment:
                self.counter += 1
        otp = self._get_otp(self._secret, counter,
                            self._n_digits, self._algorithm)
        return otp

    def compare(self, code, look_ahead=2):
        """
        Check the code to see if it's valid, comparing it to the
        code for the current counter and several future codes to
        allow for the generators getting out of sync.
        Returns True if the code is valid.

        The current counter will be synchronized to match the code
        entered.

        Args:
          code (str): The code to check
          look_ahead (int, optional): Check this many valid codes in the
                                      future of the current code
                                      (default: 2)
        """
        if look_ahead < 0:
            raise ValueError("Look-ahead must be non-negative")
        counters = [self.counter + i
                    for i in range(0, look_ahead + 1)]
        valid_codes = [self.get_otp(counter) for counter in counters]
        try:
            is_valid = [self._compare(code, valid) for valid in valid_codes]
            correct_idx = is_valid.index(True)
        except ValueError:
            return False
        else:
            self.counter = counters[correct_idx] + 1
            return True
