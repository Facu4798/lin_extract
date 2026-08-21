# TO BE MODIFIED BY ME ---------------------------------------------------------------
cosmos_root = "abfss://.../snapshots/"
spi_files = "abfss://.../spi/"
json_files = "abfss://..../query_builders"
# ------------------------------------------------------------------------------------


from __future__ import annotations
import json
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, explode_outer
from pyspark.sql.types import ArrayType, StructType


def flatten_and_explode(df: DataFrame, separator: str = "_") -> DataFrame:
    while True:
        struct_fields = [f.name for f in df.schema.fields if isinstance(f.dataType, StructType)]
        array_fields = [f.name for f in df.schema.fields if isinstance(f.dataType, ArrayType)]

        if not struct_fields and not array_fields:
            return df

        if struct_fields:
            other_names = [c for c in df.columns if c not in struct_fields]
            expanded_names = [
                f"{name}{separator}{sub.name}"
                for name in struct_fields
                for sub in df.schema[name].dataType.fields
            ]
            all_names = other_names + expanded_names
            seen: set[str] = set()
            dupes = {n for n in all_names if n in seen or seen.add(n)}
            if dupes:
                raise ValueError(
                    f"flatten_and_explode: flattening would produce duplicate "
                    f"column name(s) {sorted(dupes)} — pick a different "
                    f"separator or rename the source columns"
                )

            other_cols = [col(c) for c in other_names]
            expanded_cols = [
                col(f"{name}.{sub.name}").alias(f"{name}{separator}{sub.name}")
                for name in struct_fields
                for sub in df.schema[name].dataType.fields
            ]
            df = df.select(*other_cols, *expanded_cols)
            continue  # re-check the schema — a subfield could itself be a struct or array


        df = df.withColumn(array_fields[0], explode_outer(col(array_fields[0])))



cosmos_paths = [
    f.path
    for collection in mssparkutils.fs.ls(cosmos_root)
    for f in mssparkutils.fs.ls(collection.path)
]  # snapshots/collection/sor/

cosmos_snapshots = {
    f"{d.split('/')[-2]}_{d.split('/')[-1]}": spark.read.format("parquet").load(d)
    for d in cosmos_paths
}

for k,v in cosmos_snapshots.items():
    cosmos_snapshots[k] = flatten_and_explode(v)

for view_name, snapshot_df in cosmos_snapshots.items():
    snapshot_df.createOrReplaceTempView(view_name)


spi_files = spark.read.format("csv")\
    .option("header","true")\
    .option("inferSchema","true")\
    .load(spi_files)


sample_json = {
    "role" : "customer",
    "source" : "collection1_sor1 as c1",
    "joins" : [
        {
            "source" : "collection2_sor1 as c2",
            "on" : "c1.id = c2._id",
            "how" : "full outer"
        }
    ],
    "select" : {
        "name" : [
            "c1.customerName",
            "c2.customerName"
        ],
        "nationalId" :[
            "c1.customerNationalId",
            "c2.customerNationalId"
        ],
        "role": "customer"
    }
}




json_files = mssparkutils.fs.ls(json_files)
json_files = [f for f in json_files if f.path.endswith(".json")]
json_files = [
    "\n".join([l.value for l in spark.read.text(f).collect()]) for f in json_files
]
json_files = [json.loads(f) for f in json_files]




def _sql_string_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


dfs = []
for f in json_files:

    se = ""
    for s_name,s_exp in f["select"].items():
        if isinstance(s_exp,list):
            se += f"COALESCE({','.join(s_exp)}) AS {s_name},"
        elif isinstance(s_exp,str):
            se += f"'{s_exp}' AS {s_name},"
        elif s_exp is None:
            se += f"NULL AS {s_name}"
    se = se.rsplit(",",1)[0]
    

    q = f"SELECT {se} FROM {f['source']}"
    for j in f["joins"]:
        q += f"\n{j['how'].upper()} JOIN {j['source']} ON {j['on']}"
    dfs.append(flatten_and_explode(spark.sql(q)))

df_final = dfs[0]
if len(dfs) > 1:
    for df in dfs[1:]:
        df_final = df_final.unionByName(df, allowMissingColumns=True)

display(df_final)
