import asyncio
import traceback
import sqlalchemy
import database

async def main():
    try:
        async with database.engine.connect() as conn:
            res = await conn.execute(sqlalchemy.text("SELECT 1"))
            row = res.first()
            print("CONNECTED", row)
    except Exception as e:
        print("ERROR", repr(e))
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
