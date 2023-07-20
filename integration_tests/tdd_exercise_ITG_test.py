import unittest
from smtm import TddExercise
from unittest.mock import Mock
import requests


class TddExerciseIntegrationTests(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_initialize_correctly(self):
        ex = TddExercise()
        ex.to = "2020-02-25T06:41:00Z"
        ex.count = 60
        ex.set_period(ex.to, ex.count)
        ex.initialize_from_server()

        self.assertEqual(len(ex.data), 60)
        self.assertEqual(
            ex.data[0]["candle_date_time_utc"], "2020-02-25T06:40:00")
        self.assertEqual(
            ex.data[-1]["candle_date_time_utc"], "2020-02-25T05:41:00")
