async def _retry_loop(self):
    while True:
        due_rows = await self.fetch_due_rows()
        if due_rows:
            for row in due_rows:
                await self.process_row(row)
            if self.poll_interval_s == 0:
                await asyncio.sleep(0)  # Yield control to let other tasks run
        else:
            if self.poll_interval_s == 0:
                await asyncio.sleep(0)  # Avoid busy looping when no rows are due
            else:
                await asyncio.sleep(self.poll_interval_s)
