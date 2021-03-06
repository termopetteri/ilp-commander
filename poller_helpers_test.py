import arrow
from decimal import Decimal

from freezegun import freeze_time

from poller_helpers import median, send_ir_signal, Commands, TempTs


def test_median():
    ts1 = arrow.now()
    ts2 = ts1.shift(minutes=2)

    result_temp, result_ts = median([(Decimal(10), ts1), (Decimal(12), ts2)])

    assert result_temp == Decimal(11)
    assert result_ts == ts1.shift(minutes=1)


def test_median_list_of_temps():
    ts1 = arrow.now()
    ts2 = ts1.shift(minutes=2)
    ts3 = ts1.shift(minutes=-2)

    list1 = (
        [TempTs(Decimal(10), ts1), TempTs(Decimal(12), ts2)],
        ts3
    )

    list2 = (
        [TempTs(Decimal(9), ts1), TempTs(Decimal(11), ts2)],
        ts3
    )

    result_temp, result_ts = median([list1, list2])

    assert result_temp == [(Decimal('9.5'), ts1), (Decimal('11.5'), ts2)]
    assert result_ts == ts1


def test_send_ir_signal_fail(mocker):
    mock_email = mocker.patch('poller_helpers.email')
    mocker.patch('time.sleep')

    freeze_ts = arrow.get('2017-08-18T15:00:00+00:00')
    with freeze_time(freeze_ts.datetime):
        send_ir_signal(Commands.heat20, extra_info=['Foo1', 'Foo2'])

    mock_email.assert_called_once_with(
        'Send IR',
        '18.08.2017 18:00\nheat_20__fan_high__swing_down\nFoo1\nFoo2\nirsend: FileNotFoundError')


def test_send_ir_signal_ok(mocker):
    mock_email = mocker.patch('poller_helpers.email')
    mock_popen = mocker.patch('poller_helpers.Popen')

    freeze_ts = arrow.get('2017-08-18T15:00:00+00:00')
    with freeze_time(freeze_ts.datetime):
        send_ir_signal(Commands.heat20, extra_info=['Foo1', 'Foo2'])

    mock_email.assert_called_once_with(
        'Send IR',
        '18.08.2017 18:00\nheat_20__fan_high__swing_down\nFoo1\nFoo2')

    mock_popen.assert_called_once()


def test_command():
    assert Commands.off < Commands.heat8
    assert not Commands.off > Commands.heat8
    assert not Commands.off < Commands.off
    assert not Commands.off > Commands.off
    assert not Commands.off != Commands.off
    assert Commands.off == Commands.off
    assert Commands.off
    assert Commands.off != ''
    assert Commands.off != 234
