import pytest

from pyspark.sql import functions as F

from koheesio.spark import AnalysisException, DataFrame
from koheesio.spark.readers.delta import DeltaTableReader

pytestmark = pytest.mark.spark


def test_delta_table_reader(spark):
    df = DeltaTableReader(table="delta_test_table").read()
    df.show()
    actual = df.head().asDict()
    expected = {"id": 0}

    assert isinstance(df, DataFrame)
    assert actual == expected


def test_delta_table_cdf_reader(spark, streaming_dummy_df, random_uuid):
    table_name = f"delta_test_table_cdf_{random_uuid}"

    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {table_name} (id INT) "
        "USING DELTA "
        "TBLPROPERTIES (delta.enableChangeDataFeed = true)"
    )
    spark.sql(f"INSERT INTO {table_name} VALUES (10)")
    spark.sql(f"INSERT INTO {table_name} VALUES (0)")
    spark.sql(f"UPDATE {table_name} SET id = 100 WHERE id = 0")
    spark.sql(f"DELETE FROM {table_name} WHERE id = 10")
    spark.sql(f"UPDATE {table_name} SET id = 0 WHERE id = 100")

    cdf_reader = DeltaTableReader(table=table_name, read_change_feed=True, starting_version=0)
    df = cdf_reader.read()
    df.orderBy("_commit_timestamp").show(truncate=False)
    df.show()
    assert df.count() == 7


# FIXME: causing other tests to fail
# def test_delta_table_cdf_stream_reader(spark, checkpoint_folder, streaming_dummy_df):
#     spark.sql("INSERT INTO delta_test_table VALUES (10)")
#     spark.sql("UPDATE delta_test_table SET id = 100 WHERE id = 0")
#     spark.sql("DELETE FROM delta_test_table WHERE id = 10")
#     spark.sql("UPDATE delta_test_table SET id = 0 WHERE id = 100")
#
#     cdf_reader = DeltaTableReader(
#         table="delta_test_table",
#         read_change_feed=True,
#         ignore_changes=True,
#         streaming=True,
#         starting_version=0,
#         filter_cond=col("_change_type") != "insert",
#     )
#
#     cdf_reader.read().writeStream.format("console").start().awaitTermination(60)


def test_delta_reader_view(spark):
    reader = DeltaTableReader(table="delta_test_table")

    with pytest.raises(AnalysisException):
        _ = spark.table(reader.view)
        # In Spark remote session the above statetment will not raise an exception
        _ = spark.table(reader.view).take(1)
    reader.read()
    df = spark.table(reader.view)
    assert df.count() == 10


def test_delta_reader_failed():
    table_name = "delta_test_table"
    with pytest.raises(ValueError):
        DeltaTableReader(table=table_name, starting_version=3, starting_timestamp="2023-10-02").read()

    with pytest.raises(ValueError):
        DeltaTableReader(table=table_name, read_change_feed=True).read()


@pytest.fixture(scope="session")
def select_and_filter_table(spark):
    df = spark.createDataFrame(
        [["Michael", "SG", 6], ["Scottie", "SF", 6], ["Steve", "SG", 5], ["Tony", "F", 3], ["Dennis", "C", 5]],
        schema=["name", "role", "rings"],
    )

    df.write.format("delta").saveAsTable("select_and_filter_test")


def test_string_filter_cond(spark, select_and_filter_table):
    expected = {"name": "Michael", "rings": 6, "role": "SG"}
    reader = DeltaTableReader(table="select_and_filter_test", filter_cond="name = 'Michael'")

    actual = reader.read().head().asDict()
    assert actual == expected


@pytest.mark.parametrize(
    "filter_cond,expected",
    [
        ("""F.col("rings").eqNullSafe(3)""", {"name": "Tony", "rings": 3, "role": "F"}),
        ("""(F.col("rings") > 3) & (F.col("name").startswith("St"))""", {"name": "Steve", "rings": 5, "role": "SG"}),
    ],
)
def test_filter_cond(spark, select_and_filter_table, filter_cond, expected):
    filter_cond = eval(filter_cond)
    reader = DeltaTableReader(table="select_and_filter_test", filter_cond=filter_cond)
    actual = reader.read().head().asDict()
    assert actual == expected
    assert reader.df.select(F.col("name")).head().asDict() == {"name": expected["name"]}


@pytest.mark.parametrize(
    "columns,expected_schema",
    [
        (["name", "rings"], {"name", "rings"}),
        (["role", "rings"], {"role", "rings"}),
        (None, {"name", "rings", "role"}),
        ("name", {"name"}),
    ],
)
def test_select_columns(spark, select_and_filter_table, columns, expected_schema):
    reader = DeltaTableReader(table="select_and_filter_test", columns=columns)
    actual_schema = set(reader.read().columns)
    assert actual_schema == expected_schema


@pytest.mark.parametrize("field", ["ending_version", "endingVersion", "ending_timestamp", "endingTimestamp"])
def test_cdf_end_bound_options(spark, field):
    version = "version" in field.lower()
    value = "0" if version else "2026-01-01 00:00:00"
    option = "endingVersion" if version else "endingTimestamp"
    reader = DeltaTableReader(table="delta_test_table", read_change_feed=True, starting_version="0", **{field: value})
    assert reader.get_options()[option] == value
    assert reader.model_dump(by_alias=True)[option] == value


@pytest.mark.parametrize(
    "options, message",
    [
        ({"ending_version": "2", "ending_timestamp": "2026-01-01"}, "not both"),
        ({"ending_version": "2", "streaming": True}, "batch change data feed"),
        ({"endingTimestamp": "2026-01-01", "streaming": True}, "batch change data feed"),
        ({"ending_version": "2", "read_change_feed": False}, "batch change data feed"),
        ({"endingTimestamp": "2026-01-01", "read_change_feed": False}, "batch change data feed"),
    ],
)
def test_cdf_end_bounds_reject_unsupported_modes(spark, options, message):
    params = {"table": "delta_test_table", "read_change_feed": True, "starting_version": "0", **options}
    with pytest.raises(ValueError, match=message):
        DeltaTableReader(**params)


def test_cdf_omitted_end_bounds_preserve_options(spark):
    reader = DeltaTableReader(table="delta_test_table", read_change_feed=True, starting_version="0")
    assert reader.get_options() == {"readChangeFeed": "true", "startingVersion": "0"}


@pytest.fixture
def bounded_cdf_table(spark, random_uuid):
    table = f"bounded_cdf_{random_uuid}"
    spark.sql(f"CREATE TABLE {table} (id INT) USING DELTA TBLPROPERTIES (delta.enableChangeDataFeed = true)")
    try:
        for value in (10, 20, 30):
            spark.sql(f"INSERT INTO {table} VALUES ({value})")
        yield table
    finally:
        spark.sql(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.parametrize("boundary_kind", ["version", "timestamp"])
def test_cdf_bounded_read_matches_native_delta(spark, bounded_cdf_table, boundary_kind):
    table = bounded_cdf_table
    if boundary_kind == "version":
        start, end = "1", "2"
        start_option, end_option = "startingVersion", "endingVersion"
    else:
        history = {row.version: row.timestamp for row in spark.sql(f"DESCRIBE HISTORY {table}").collect()}
        start, end = (history[version].isoformat(sep=" ", timespec="milliseconds") for version in (1, 2))
        start_option, end_option = "startingTimestamp", "endingTimestamp"

    options = {start_option: start, end_option: end}
    actual = DeltaTableReader(table=table, read_change_feed=True, **options).read()
    native = spark.read.format("delta").option("readChangeFeed", "true").options(**options).table(table)
    columns = ["id", "_commit_version", "_change_type"]
    actual_rows = actual.select(*columns).orderBy("_commit_version").collect()
    assert actual_rows == native.select(*columns).orderBy("_commit_version").collect()
    assert [(row.id, row._commit_version) for row in actual_rows] == [(10, 1), (20, 2)]
    unbounded = DeltaTableReader(table=table, read_change_feed=True, starting_version="1").read()
    assert unbounded.count() == 3
