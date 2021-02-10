import pytest

pytestmark = pytest.mark.asyncio


async def test_funny_name(aconn):
    cur = await aconn.cursor("1-2-3")
    await cur.execute("select generate_series(1, 3) as bar")
    assert await cur.fetchall() == [(1,), (2,), (3,)]
    assert cur.name == "1-2-3"


async def test_description(aconn):
    cur = await aconn.cursor("foo")
    assert cur.name == "foo"
    await cur.execute("select generate_series(1, 10) as bar")
    assert len(cur.description) == 1
    assert cur.description[0].name == "bar"
    assert cur.description[0].type_code == cur.adapters.types["int4"].oid
    assert cur.pgresult.ntuples == 0


async def test_close(aconn, recwarn):
    cur = await aconn.cursor("foo")
    await cur.execute("select generate_series(1, 10) as bar")
    await cur.close()
    assert cur.closed

    assert not await (
        await aconn.execute("select * from pg_cursors where name = 'foo'")
    ).fetchone()
    del cur
    assert not recwarn


async def test_context(aconn, recwarn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, 10) as bar")

    assert cur.closed
    assert not await (
        await aconn.execute("select * from pg_cursors where name = 'foo'")
    ).fetchone()
    del cur
    assert not recwarn


async def test_warn_close(aconn, recwarn):
    cur = await aconn.cursor("foo")
    await cur.execute("select generate_series(1, 10) as bar")
    del cur
    assert ".close()" in str(recwarn.pop(ResourceWarning).message)


async def test_fetchone(aconn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (2,))
        assert await cur.fetchone() == (1,)
        assert await cur.fetchone() == (2,)
        assert await cur.fetchone() is None


async def test_fetchmany(aconn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (5,))
        assert await cur.fetchmany(3) == [(1,), (2,), (3,)]
        assert await cur.fetchone() == (4,)
        assert await cur.fetchmany(3) == [(5,)]
        assert await cur.fetchmany(3) == []


async def test_fetchall(aconn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        assert await cur.fetchall() == [(1,), (2,), (3,)]
        assert await cur.fetchall() == []

    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        assert await cur.fetchone() == (1,)
        assert await cur.fetchall() == [(2,), (3,)]
        assert await cur.fetchall() == []


async def test_rownumber(aconn):
    cur = await aconn.cursor("foo")
    assert cur.rownumber is None

    await cur.execute("select 1 from generate_series(1, 42)")
    assert cur.rownumber == 0

    await cur.fetchone()
    assert cur.rownumber == 1
    await cur.fetchone()
    assert cur.rownumber == 2
    await cur.fetchmany(10)
    assert cur.rownumber == 12
    await cur.fetchall()
    assert cur.rownumber == 42


async def test_iter(aconn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        recs = []
        async for rec in cur:
            recs.append(rec)
    assert recs == [(1,), (2,), (3,)]

    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        assert await cur.fetchone() == (1,)
        recs = []
        async for rec in cur:
            recs.append(rec)
    assert recs == [(2,), (3,)]


async def test_iter_rownumber(aconn):
    async with await aconn.cursor("foo") as cur:
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        async for row in cur:
            assert cur.rownumber == row[0]


async def test_itersize(aconn, acommands):
    async with await aconn.cursor("foo") as cur:
        assert cur.itersize == 100
        cur.itersize = 2
        await cur.execute("select generate_series(1, %s) as bar", (3,))
        acommands.popall()  # flush begin and other noise

        async for rec in cur:
            pass
        cmds = acommands.popall()
        assert len(cmds) == 2
        for cmd in cmds:
            assert ("fetch forward 2") in cmd.lower()


async def test_scroll(aconn):
    cur = await aconn.cursor("tmp")
    with pytest.raises(aconn.ProgrammingError):
        await cur.scroll(0)

    await cur.execute("select generate_series(0,9)")
    await cur.scroll(2)
    assert await cur.fetchone() == (2,)
    await cur.scroll(2)
    assert await cur.fetchone() == (5,)
    await cur.scroll(2, mode="relative")
    assert await cur.fetchone() == (8,)
    await cur.scroll(9, mode="absolute")
    assert await cur.fetchone() == (9,)

    with pytest.raises(ValueError):
        await cur.scroll(9, mode="wat")


async def test_scrollable(aconn):
    curs = await aconn.cursor("foo")
    await curs.execute("select generate_series(0, 5)")
    await curs.scroll(5)
    for i in range(4, -1, -1):
        await curs.scroll(-1)
        assert i == (await curs.fetchone())[0]
        await curs.scroll(-1)


async def test_non_scrollable(aconn):
    curs = await aconn.cursor("foo")
    await curs.execute("select generate_series(0, 5)", scrollable=False)
    await curs.scroll(5)
    with pytest.raises(aconn.OperationalError):
        await curs.scroll(-1)


async def test_steal_cursor(aconn):
    cur1 = await aconn.cursor()
    await cur1.execute(
        "declare test cursor without hold for select generate_series(1, 6)"
    )

    cur2 = await aconn.cursor("test")
    # can call fetch without execute
    assert await cur2.fetchone() == (1,)
    assert await cur2.fetchmany(3) == [(2,), (3,), (4,)]
    assert await cur2.fetchall() == [(5,), (6,)]
