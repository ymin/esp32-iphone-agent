import asyncio

import pytest

from hidagent.device import Device, DeviceError


def test_timeout_grows_with_move_distance_and_text_length():
    assert Device._timeout("click") == 5
    assert Device._timeout("move 1000 -20") > Device._timeout("move 10 10") >= 5
    assert Device._timeout("type " + "x" * 40) > Device._timeout("type hi")


def test_send_without_board_fails_fast():
    with pytest.raises(DeviceError, match="not connected"):
        asyncio.run(Device().send("ping"))


def test_round_trip_with_a_fake_board():
    async def scenario():
        device = Device()
        server = await device.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def board():  # answers every command like the firmware does
            while line := await reader.readline():
                writer.write(b"ok\n" if line.strip() != b"bad" else b"err unknown command\n")
                await writer.drain()

        task = asyncio.create_task(board())
        assert await device.wait_connected(2)
        await device.send("ping")
        with pytest.raises(DeviceError, match="unknown command"):
            await device.send("bad")
        assert device.links == 1
        task.cancel()
        writer.close()
        server.close()

    asyncio.run(scenario())


async def connect_board(port: int, hello: bytes):
    """A fake board that greets with `hello` and answers every command with ok."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(hello)
    await writer.drain()

    async def answer():
        while await reader.readline():
            writer.write(b"ok\n")
            await writer.drain()

    return asyncio.create_task(answer()), writer


def scenario_with_token(token, hellos):
    """Boards greet in turn; returns (links, whether the last board accepted is still connected, the device)."""

    async def run():
        device = Device(token=token)
        server = await device.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        boards = []
        for hello in hellos:
            boards.append(await connect_board(port, hello))
            await asyncio.sleep(0.2)
        connected = device.connected and (await device.send("ping")) is None
        for task, writer in boards:
            task.cancel()
            writer.close()
        server.close()
        return device.links, connected

    return asyncio.run(run())


def test_board_with_the_right_token_connects():
    assert scenario_with_token("s3cret", [b"hello power-on 120 s3cret\n"]) == (1, True)


@pytest.mark.parametrize("stranger", [b"hello power-on 120 wrong\n", b"hello power-on 120\n", b"ping\n", b""])
def test_stranger_without_the_token_never_replaces_the_board(stranger):
    assert scenario_with_token("s3cret", [b"hello power-on 120 s3cret\n", stranger]) == (1, True)


def test_stranger_alone_is_not_a_board():
    links, connected = scenario_with_token("s3cret", [b"hello power-on 120 wrong\n"])
    assert links == 0 and not connected


def test_board_slow_to_greet_through_a_tunnel_connects():
    # behind Tailscale Funnel the link opens when the board's TLS handshake starts; over 5G that takes seconds
    async def run():
        device = Device(token="s3cret")
        server = await device.serve("127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await asyncio.sleep(2)
        writer.write(b"hello power-on 120 s3cret\n")
        connected = await device.wait_connected(2)
        writer.close()
        server.close()
        return connected

    assert asyncio.run(run())


def test_without_a_token_any_board_connects():
    assert scenario_with_token(None, [b"hello power-on 120\n"]) == (1, True)


def test_token_never_reaches_the_log(caplog):
    caplog.set_level("DEBUG")
    scenario_with_token("s3cret", [b"hello power-on 120 s3cret\n", b"hello power-on 120 wrong-guess\n"])
    assert "s3cret" not in caplog.text and "wrong-guess" not in caplog.text
