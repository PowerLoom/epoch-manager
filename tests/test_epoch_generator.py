


import asyncio
import uvloop
from epoch_generator import EpochGenerator



def main():
    """Spin up the ticker process in event loop"""
    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)

    ticker_process = EpochGenerator()
    loop.run_until_complete(ticker_process.run())


if __name__ == "__main__":
    main()
    