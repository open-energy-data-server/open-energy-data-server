# Exporting Data from the Open Energy Data Server using HTTP via the PostgREST API
Dataset:
Londondatastore.
Average data from london smartmeter measurements.

## HTTP using Python requests

```python
import requests
import pandas as pd

url = "https://localhost:6432/consumption"
headers = {"Accept": "application/json", "Accept-Profile": "londondatastore"}
params = {
    "limit": 10,
    "DateTime": "gte.'2012-01-01'",
    "DateTime": "lte.'2013-01-01'",
    "select": "DateTime,power.sum()",
}

response = requests.get(url, headers=headers, params=params)
df = pd.DataFrame.from_records(response.json(), index="DateTime")
df.index = pd.to_datetime(df.index)
df = df.resample("1h").mean()
df.to_csv("londondatastore_pgrst.csv")
```

## HTTP using javascript fetch

```
const fetch = require('node-fetch');
const fs = require('fs');
const { DateTime } = require('luxon');

const url = "https://localhost:6432/consumption";
const headers = {
    "Accept": "application/json",
    "Accept-Profile": "londondatastore"
};
const params = {
    "limit": 10,
    "DateTime": "gte.'2012-01-01'",
    "DateTime": "lte.'2013-01-01'",
    "select": "DateTime,power.sum()",
};

fetch(url, { headers, params })
    .then(response => response.json())
    .then(data => {
        const df = data.map(row => ({
            DateTime: DateTime.fromISO(row.DateTime),
            power: row.power
        }));
        fs.writeFileSync("londondatastore_pgrst.csv", df);
    });
```
