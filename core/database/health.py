from dataclasses import dataclass

from psycopg import Error, connect

from config import DatabaseSettings


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseInfo:
    database: str
    schema: str


def check_database() -> DatabaseInfo:
    try:
        with connect(
            host=DatabaseSettings.HOST,
            port=DatabaseSettings.PORT,
            user=DatabaseSettings.USERNAME,
            password=DatabaseSettings.PASSWORD,
            dbname=DatabaseSettings.DATABASE,
            connect_timeout=5,
        ) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_schema()")
            row = cursor.fetchone()
    except Error as error:
        raise DatabaseError("PostgreSQL 连接失败") from error
    if row is None:
        raise DatabaseError("PostgreSQL 未返回当前数据库和 schema")
    database_name, schema_name = map(str, row)
    if database_name != DatabaseSettings.DATABASE:
        raise DatabaseError(f"应用数据库错误: 期望 {DatabaseSettings.DATABASE}，实际 {database_name}")
    return DatabaseInfo(database=database_name, schema=schema_name)
