import pytest
from faststream import FastStream, TestApp
from faststream.rabbit import RabbitBroker, TestRabbitBroker

app = FastStream(RabbitBroker())
lifespan_events = []


@app.after_startup
async def handle():
    lifespan_events.append("startup")


@app.after_shutdown
async def handle_shutdown():
    lifespan_events.append("shutdown")


@pytest.mark.asyncio
async def test_lifespan():
    lifespan_events.clear()

    async with (
        TestRabbitBroker(app.broker, connect_only=True),
        TestApp(app),
    ):
        assert lifespan_events == ["startup"]

    assert lifespan_events == ["startup", "shutdown"]
