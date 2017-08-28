# coding=utf-8
import time
from decimal import Decimal
from functools import wraps
from statistics import mean

import arrow
import requests
import xmltodict
from dateutil import tz

import config
from poller_helpers import Commands, logger, send_ir_signal, timing, get_most_recent_message, get_temp_from_sheet, \
    median
from states import State


class RequestCache:
    _cache = {}

    @classmethod
    def put(cls, name, stale_after, content):
        stale_after = max([stale_after, arrow.now().shift(minutes=10)])
        cls._cache[name] = (stale_after, content)

    @classmethod
    def get(cls, name, ignore_stale_check=False):
        if name in cls._cache:
            content = cls._cache[name][1]

            if ignore_stale_check:
                return content

            stale_after = cls._cache[name][0]
            if arrow.now() <= stale_after:
                return content

        return None

    @classmethod
    def reset(cls):
        cls._cache.clear()


def caching(cache_name):
    def caching_inner(f):
        @wraps(f)
        def caching_wrap(*args, **kw):
            rq = RequestCache()
            result = rq.get(cache_name)
            if result:
                logger.debug('func:%r args:[%r, %r] cache hit with result: %r' % (f.__name__, args, kw, result))
            else:
                logger.debug('func:%r args:[%r, %r] cache miss' % (f.__name__, args, kw))
                result = f(*args, **kw)
                if result:
                    temp, ts = result
                    if ts is not None:
                        logger.debug('func:%r args:[%r, %r] storing with result: %r' % (f.__name__, args, kw, result))
                        rq.put(cache_name, ts.shift(minutes=config.CACHE_TIMES.get(cache_name, 60)), result)
            return result
        return caching_wrap
    return caching_inner


@timing
@caching(cache_name='ulkoilma')
def receive_ulkoilma_temperature():
    temp, ts = get_temp_from_sheet(sheet_index=2)

    if ts is not None and temp is not None:
        ts = arrow.get(ts, 'DD.MM.YYYY klo HH:mm').replace(tzinfo=tz.gettz(config.TIMEZONE))
        temp = Decimal(temp)

    logger.info('%s %s', temp, ts)
    return temp, ts


@timing
@caching(cache_name='wc')
def receive_wc_temperature():
    temp, ts = get_temp_from_sheet(sheet_index=0)

    if ts is not None and temp is not None:
        ts = arrow.get(ts, 'DD.MM.YYYY klo HH:mm').replace(tzinfo=tz.gettz(config.TIMEZONE))
        temp = Decimal(temp)

    logger.info('%s %s', temp, ts)
    return temp, ts


@timing
@caching(cache_name='fmi')
def receive_fmi_temperature():
    temp, ts = None, None

    try:
        starttime = arrow.now().shift(hours=-1).to('UTC').format('YYYY-MM-DDTHH:mm:ss') + 'Z'
        result = requests.get(
            'http://data.fmi.fi/fmi-apikey/{key}/wfs?request=getFeature&storedquery_id=fmi::observations::weather'
            '::simple&place={place}&parameters=temperature&starttime={starttime}'.format(
                key=config.FMI_KEY, place=config.FMI_LOCATION, starttime=starttime))
    except Exception as e:
        logger.exception(e)
    else:
        if result.status_code != 200:
            logger.error('%d: %s' % (result.status_code, result.content))
        else:
            temp_data = xmltodict.parse(result.content)['wfs:FeatureCollection']['wfs:member'][-1]['BsWfs:BsWfsElement']
            ts = arrow.get(temp_data['BsWfs:Time']).to(config.TIMEZONE)
            temp = Decimal(temp_data['BsWfs:ParameterValue'])

    logger.info('%s %s', temp, ts)
    return temp, ts


@timing
@caching(cache_name='open_weather_map')
def receive_open_weather_map_temperature():
    temp, ts = None, None

    try:
        result = requests.get(
            'http://api.openweathermap.org/data/2.5/weather?q={place}&units=metric&appid={key}'.format(
                key=config.OPEN_WEATHER_MAP_KEY, place=config.OPEN_WEATHER_MAP_LOCATION))
    except Exception as e:
        logger.exception(e)
    else:
        if result.status_code != 200:
            logger.error('%d: %s' % (result.status_code, result.content))
        else:
            result_json = result.json()
            temp = Decimal(result_json['main']['temp'])
            ts = arrow.get(result_json['dt']).to(config.TIMEZONE)

    logger.info('%s %s', temp, ts)
    return temp, ts


@timing
@caching(cache_name='yr.no')
def receive_yr_no_forecast_min_temperature(hours=None):
    temp, ts = None, None
    rq = RequestCache()

    try:
        result = requests.get(
            'http://www.yr.no/place/{place}/forecast_hour_by_hour.xml'.format(place=config.YR_NO_LOCATION))
    except Exception as e:
        logger.exception(e)
        result = None

    if result:
        if result.status_code != 200:
            logger.error('%d: %s' % (result.status_code, result.content))
        else:
            d = xmltodict.parse(result.content)
            timezone = d['weatherdata']['location']['timezone']['@id']

            if hours is None:
                until_ts = None
            else:
                until_ts = arrow.now().shift(hours=hours)

            time_elements = [
                t
                for t
                in d['weatherdata']['forecast']['tabular']['time']
                if until_ts is None or arrow.get(t['@from']).replace(tzinfo=timezone) < until_ts
            ]

            temp = min(Decimal(t['temperature']['@value']) for t in time_elements)
            min_datetime = arrow.get(min(t['@from'] for t in time_elements)).replace(tzinfo=timezone)
            max_datetime = arrow.get(max(t['@to'] for t in time_elements)).replace(tzinfo=timezone)
            ts = arrow.now()

            logger.info('Min forecast temp: %s between %s and %s', temp, min_datetime, max_datetime)
    else:
        logger.info('Request failed. Getting result from cache.')
        temp, ts = rq.get('yr.no', ignore_stale_check=True)

        stale_after = ts.shift(hours=24)
        if arrow.now() > stale_after:
            temp, ts = None, None

    logger.info('%s %s', temp, ts)
    return temp, ts


def func_name(func):
    if hasattr(func, '__name__'):
        return func.__name__
    else:
        return func._mock_name


class Temperatures:
    MAX_TS_DIFF_MINUTES = 60

    @classmethod
    def get_temp(cls, functions: list, max_ts_diff=None, **kwargs):
        if max_ts_diff is None:
            max_ts_diff = cls.MAX_TS_DIFF_MINUTES

        temperatures = []

        for func in functions:
            temp, ts = func(**kwargs)
            if temp is not None:
                if ts is None:
                    temperatures.append((temp, ts))
                else:
                    seconds = (arrow.now() - ts).total_seconds()
                    if abs(seconds) < 60 * max_ts_diff:
                        temperatures.append((temp, ts))
                    else:
                        logger.info('Discarding temperature %s, temp: %s, temp time: %s', func_name(func), temp, ts)

        return median(temperatures)


def target_inside_temperature(outside_temp: Decimal, allowed_min_inside_temp: Decimal):

    def foo(result: Decimal, count: int) -> Decimal:
        if count > 0:
            inside_outside_diff = mean([result - outside_temp, allowed_min_inside_temp - outside_temp])
            new_result = \
                config.COOLING_RATE_PER_HOUR_PER_TEMPERATURE_DIFF \
                * inside_outside_diff \
                * config.COOLING_TIME_BUFFER \
                + allowed_min_inside_temp
            return foo(new_result, count - 1)
        return result

    return max(foo(allowed_min_inside_temp, 3), config.MINIMUM_INSIDE_TEMP)


class Auto(State):

    min_forecast_temp = None
    last_command = None
    last_command_send_time = time.time()

    def run(self, payload, version=2):
        if version == 1:
            next_command, extra_info = self.version_1()
        elif version == 2:
            next_command, extra_info = self.version_2()
        else:
            raise ValueError(version)

        if Auto.last_command is not None:
            logger.debug('Last auto command sent %d minutes ago', (time.time() - Auto.last_command_send_time) / 60.0)

        if Auto.last_command != next_command:
            Auto.last_command = next_command
            Auto.last_command_send_time = time.time()
            send_ir_signal(next_command, extra_info=extra_info)

        return get_most_recent_message(once=True)

    @staticmethod
    def version_1():
        inside_temp = Temperatures.get_temp([receive_wc_temperature])[0]
        logger.info('Inside temperature: %s', inside_temp)
        extra_info = ['Inside temperature: %s' % inside_temp]

        if inside_temp is None or inside_temp < 8:

            outside_temp = Temperatures.get_temp([
                receive_ulkoilma_temperature, receive_fmi_temperature, receive_open_weather_map_temperature])[0]

            extra_info.append('Outside temperature: %s' % outside_temp)

            if outside_temp is not None:
                logger.info('Outside temperature: %.1f', outside_temp)

                if outside_temp > 0:
                    next_command = Commands.off
                elif 0 >= outside_temp > -15:
                    next_command = Commands.heat8
                elif -15 >= outside_temp > -20:
                    next_command = Commands.heat10
                elif -20 >= outside_temp > -25:
                    next_command = Commands.heat16
                else:
                    next_command = Commands.heat20

            else:
                next_command = Commands.heat16  # Don't know the temperature so heat up just in case
                logger.error('Got no temperatures at all. Setting %s', next_command)
                extra_info.append('Got no temperatures at all.')

        else:
            next_command = Commands.off  # No need to heat

        return next_command, extra_info

    @staticmethod
    def version_2():
        Auto.min_forecast_temp = Temperatures.get_temp([receive_yr_no_forecast_min_temperature], hours=24)[0]
        allowed_min_inside_temp = Decimal(1)
        extra_info = ['Forecast min temperature: %s' % Auto.min_forecast_temp]

        outside_temp = Temperatures.get_temp([
            receive_ulkoilma_temperature, receive_fmi_temperature, receive_open_weather_map_temperature])[0]
        logger.info('Outside temperature: %s', outside_temp)
        extra_info.append('Outside temperature: %s' % outside_temp)

        if outside_temp is None:
            if Auto.min_forecast_temp is not None:
                outside_temp = Auto.min_forecast_temp
                logger.info('Using forecast: %s', Auto.min_forecast_temp)
                extra_info.append('Using forecast: %s' % Auto.min_forecast_temp)
            else:
                outside_temp = Decimal(-10)
                logger.info('Using predefined outside temperature: %s', outside_temp)
                extra_info.append('Using predefined outside temperature: %s' % outside_temp)

        target_inside_temp = target_inside_temperature(outside_temp, allowed_min_inside_temp)
        logger.info('Target inside temperature: %s', target_inside_temp)
        extra_info.append('Target inside temperature: %s' % target_inside_temp.quantize(Decimal('.1')))

        inside_temp = Temperatures.get_temp([receive_wc_temperature])[0]
        logger.info('Inside temperature: %s', inside_temp)
        extra_info.append('Inside temperature: %s' % inside_temp)

        if inside_temp is not None and outside_temp is not None and inside_temp > outside_temp:
            inside_outside_diff = mean([inside_temp - outside_temp, allowed_min_inside_temp - outside_temp])
            buffer = (inside_temp - allowed_min_inside_temp) / (
                config.COOLING_RATE_PER_HOUR_PER_TEMPERATURE_DIFF * inside_outside_diff)
            if buffer >= 0:
                buffer = buffer.quantize(Decimal('.1'))
                logger.info('Current buffer: %s h', buffer)
                extra_info.append('Current buffer: %s h' % buffer)

        if inside_temp is not None:
            if outside_temp < target_inside_temp and inside_temp < target_inside_temp:
                next_command = Commands.find_command_just_above_temp(target_inside_temp)
            else:
                next_command = Commands.off
        else:
            if outside_temp < target_inside_temp:
                next_command = Commands.find_command_just_above_temp(target_inside_temp)
            else:
                next_command = Commands.off

        return next_command, extra_info

    def nex(self, payload):
        from states.manual import Manual

        if payload:
            if payload['command'] == 'auto':
                return Auto
            else:
                Auto.last_command = None  # Clear last command so Auto sends command after Manual
                return Manual
        else:
            return Auto
