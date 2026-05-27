import asyncio

async def ping_host(host, semaphore):
    async with semaphore:
        # Pinging with 1 packet (-c 1) and 1s timeout (-W 1)
        # Using subprocess to call the system ping
        proc = await asyncio.create_subprocess_exec(
            'ping', '-c', '1', '-W', '1', host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Wait for the process to complete
        await proc.wait()
        
        if proc.returncode == 0:
            print(f"[UP] {host}")
        else:
            print(f"[DOWN] {host}")

async def main():
    # Set the bouncer to k=*
    semaphore = asyncio.Semaphore(30)
    
    # List of targets
    hosts = ["8.8.8.8", "1.1.1.1", "1.0.0.1"]
    
    # Fire all tasks at once; the semaphore will govern the flow
    tasks = [ping_host(h, semaphore) for h in hosts]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

