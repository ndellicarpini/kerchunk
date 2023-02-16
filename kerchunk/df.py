import base64
import numpy as np
import ujson
import os
import pandas as pd
import fsspec

from kerchunk.utils import templateize


def get_variables(keys):
    """Get list of variable names from references.

    Parameters
    ----------
    keys : list of str
        kerchunk references keys

    Returns
    -------
    fields : list of str
        List of variable names.
    """
    fields = []
    for k in keys:
        if "/" in k:
            name, chunk = k.split("/")
            if name not in fields:
                fields.append(name)
            else:
                continue
        else:
            fields.append(k)
    return fields


def normalize_json(json_obj):
    """Normalize json representation as bytes

    Parameters
    ----------
    json_obj : str, bytes, dict, list
        JSON data for parquet file to be written.
    """
    if not isinstance(json_obj, str):
        json_obj = ujson.dumps(json_obj)
    if not isinstance(json_obj, bytes):
        json_obj = json_obj.encode()
    return json_obj


def write_json(fname, json_obj):
    """Write references into a parquet file.

    Parameters
    ----------
    fname : str
        Output filename.
    json_obj : str, bytes, dict, list
        JSON data for parquet file to be written.
    """
    json_obj = normalize_json(json_obj)
    with open(fname, "wb") as f:
        f.write(json_obj)


def make_parquet_store(
    store_name, refs, row_group_size=1000, engine="fastparquet", **kwargs
):
    """Write references as a store of parquet files with multiple row groups.
    The directory structure should mimic a normal zarr store but instead of standard chunk
    keys, references are saved as parquet dataframes with multiple row groups.

    Parameters
    ----------
    store_name : str
        Name of parquet store.
    refs : dict
        Kerchunk references
    row_group_size : int, optional
        Number of references to store in each reference file (default 1000)
    engine : {'fastparquet', 'pyarrow'}
        Library to use for writing parquet files.
    **kwargs : dict, optional
        Keyword arguments passed to parquet engine of choice.
    """
    if not os.path.exists(store_name):
        os.makedirs(store_name)
    if "refs" in refs:
        refs = refs["refs"]
    write_json(
        os.path.join(store_name, ".row_group_size"), dict(row_group_size=row_group_size)
    )
    fields = get_variables(refs)
    for field in fields:
        data = []
        field_path = os.path.join(store_name, field)
        if field.startswith("."):
            # zarr metadata keys (.zgroup, .zmetadata, etc)
            write_json(field_path, refs[field])
        else:
            if not os.path.exists(field_path):
                os.makedirs(field_path)
            # Read the variable zarray metadata to determine number of chunks
            zarray = ujson.loads(refs[f"{field}/.zarray"])
            chunk_sizes = np.array(zarray["shape"]) / np.array(zarray["chunks"])
            chunk_numbers = [np.arange(n) for n in chunk_sizes]
            if chunk_sizes.size != 0:
                nums = np.asarray(pd.MultiIndex.from_product(chunk_numbers).codes).T
            else:
                nums = np.array([0])
            nchunks = nums.shape[0]
            nmissing = 0
            for metakey in [".zarray", ".zattrs"]:
                key = f"{field}/{metakey}"
                write_json(os.path.join(field_path, metakey), refs[key])
            for i in range(nchunks):
                chunk_id = ".".join(nums[i].astype(str))
                key = f"{field}/{chunk_id}"
                # Make note if expected number of chunks differs from actual
                # number found in references
                if key not in refs:
                    nmissing += 1
                    data.append(None)
                else:
                    data.append(normalize_json(refs[key]))
            if nmissing:
                print(
                    f"Warning: Chunks missing for field {field}. "
                    f"Expected: {nchunks}, Found: {nchunks - nmissing}"
                )
            # Need to pad extra rows so total number divides row_group_size evenly
            extra_rows = row_group_size - nchunks % row_group_size
            for i in range(extra_rows):
                data.append(None)
            # The convention for parquet files is
            # <store_name>/<field_name>/refs.parq
            out_path = os.path.join(field_path, "refs.parq")
            if engine == "pyarrow":
                import pyarrow as pa
                import pyarrow.parquet as pq

                table = pa.Table.from_arrays([data], names=["data"])
                pq.write_table(
                    table,
                    out_path,
                    row_group_size=row_group_size,
                    write_statistics=False,
                    **kwargs,
                )
            else:
                import fastparquet

                df = pd.DataFrame(dict(data=data))
                fastparquet.write(
                    out_path,
                    df,
                    row_group_offsets=row_group_size,
                    stats=False,
                    **kwargs,
                )


# example from preffs's README'
df = pd.DataFrame(
    {
        "key": ["a/b", "a/b", "b"],
        "path": ["a.dat", "b.dat", None],
        "offset": [123, 12, 0],
        "size": [12, 17, 0],
        "raw": [None, None, b"data"],
    }
)


def _proc_raw(r):
    if not isinstance(r, bytes):
        r = r.encode()
    if r.startswith(b"base64:"):
        return base64.b64decode(r[7:])
    return r


def refs_to_dataframe(
    refs,
    url,
    storage_options=None,
    partition=False,
    template_length=10,
    dict_fraction=0.1,
    min_refs=100,
):
    """Transform JSON/dict references to parquet storage

    This function should produce much smaller on-disk size for any large reference set,
    and much better memory footprint when loaded wih fsspec's DFReferenceFileSystem.

    Parameters
    ----------
    refs: str | dict
        Location of a JSON file containing references or a reference set already loaded
        into memory. It will get processed by the standard referenceFS, to normalise
        any templates, etc., it might contain.
    url: str
        Location for the output, together with protocol. If partition=True, this must
        be a writable directory.
    storage_options: dict | None
        Passed to fsspec when for writing the parquet.
    partition: bool
        If True, split out the references into "metadata" and separate files for each of
        the variables within the output directory.
    template_length: int
        Controls replacing a common prefix amongst reference URLs. If non-zero (in which
        case no templating is done), finds and replaces the common prefix to URLs within
        an output file (see :func:`kerchunk.utils.templateize`). If the URLs are
        dict encoded, this step is not attempted.
    dict_fraction: float
        Use categorical/dict encoding if the number of unique URLs / total number of URLs
        is is smaller than this number.
    min_refs: int
        If any variables have fewer entries than this number, they will be included in
        "metadata" - this is typically the coordinates that you want loaded immediately
        upon opening a dataset anyway. Ignored if partition is False.
    """
    # normalise refs (e.g., for templates)
    fs = fsspec.filesystem("reference", fo=refs)
    refs = fs.references

    df = pd.DataFrame(
        {
            "key": list(refs),
            # TODO: could get unique values using set() here and make categorical
            #  columns with pd.Categorical.from_codes if it meets criterion
            "path": [r[0] if isinstance(r, list) else None for r in refs.values()],
            "offset": [
                r[1] if isinstance(r, list) and len(r) > 1 else 0 for r in refs.values()
            ],
            "size": pd.Series(
                [
                    r[2] if isinstance(r, list) and len(r) > 1 else 0
                    for r in refs.values()
                ],
                dtype="int32",
            ),
            "raw": [
                _proc_raw(r) if not isinstance(r, list) else None for r in refs.values()
            ],
        }
    )
    # recoup memory
    fs.clear_instance_cache()
    del fs, refs

    if partition is False:
        templates = None
        haspath = ~df["path"].isna()
        nhaspath = haspath.sum()
        if (
            dict_fraction
            and nhaspath
            and (df["path"][haspath].nunique() / haspath.sum()) < dict_fraction
        ):
            df["path"] = df["path"].astype("category")
        elif template_length:
            templates, urls = templateize(
                df["path"][haspath], min_length=template_length
            )
            df.loc[haspath, "path"] = urls
        df.to_parquet(
            url,
            storage_options=storage_options,
            index=False,
            object_encoding={"raw": "bytes", "key": "utf8", "path": "utf8"},
            stats=["key"],
            has_nulls=["path", "raw"],
            compression="zstd",
            engine="fastparquet",
            custom_metadata=templates or None,
        )
    else:
        ismeta = df.key.str.contains(".z")
        extra_inds = []
        gb = df[~ismeta].groupby(df.key.map(lambda s: s.split("/", 1)[0]))
        prefs = {"metadata"}
        for prefix, subdf in gb:
            if len(subdf) < min_refs:
                ind = ismeta[~ismeta].iloc[gb.indices[prefix]].index
                extra_inds.extend(ind.tolist())
                prefs.add(prefix)
                continue
            subdf["key"] = subdf.key.str.slice(len(prefix) + 1, None)
            templates = None
            haspath = ~subdf["path"].isna()
            nhaspath = haspath.sum()
            if (
                dict_fraction
                and nhaspath
                and (subdf["path"][haspath].nunique() / haspath.sum()) < dict_fraction
            ):
                subdf["path"] = subdf["path"].astype("category")
            elif template_length:
                templates, urls = templateize(
                    subdf["path"][haspath], min_length=template_length
                )
                subdf.loc[haspath, "path"] = urls

            subdf.to_parquet(
                f"{url}/{prefix}.parq",
                storage_options=storage_options,
                index=False,
                object_encoding={"raw": "bytes", "key": "utf8", "path": "utf8"},
                stats=["key"],
                has_nulls=["path", "raw"],
                compression="zstd",
                engine="fastparquet",
                custom_metadata=templates or None,
            )
        ismeta[extra_inds] = True
        df[ismeta].to_parquet(
            f"{url}/metadata.parq",
            storage_options=storage_options,
            index=False,
            object_encoding={"raw": "bytes", "key": "utf8", "path": "utf8"},
            stats=["key"],
            has_nulls=["path", "raw"],
            compression="zstd",
            engine="fastparquet",
            custom_metadata={"prefs": str(prefs)},
        )
