# Local OpenStreetMap Setup — Bangalore (100km radius)

A complete guide to running a self-hosted OpenStreetMap tile server and REST API
for a region around Bangalore, using Docker.

**Stack:** Docker + osmium (data clipping) + PostGIS (storage) + tile server (port 8080) + PostgREST API (port 3001)

---

## 1. Install prerequisites

```bash
# osmium-tool — used to clip large OSM extracts down to a specific region
sudo apt install osmium-tool

# Docker Engine (official repo — avoids conflicts with Ubuntu's docker.io package)
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# (Optional) run docker without sudo
sudo usermod -aG docker $USER
newgrp docker
```

> **Note:** If you hit `Conflicts: containerd` errors, don't install `docker.io` —
> use Docker's official `docker-ce` packages instead, as above.

---

## 2. Download OSM data

Get the full India extract (needed for a 100km radius, since it crosses state borders):

```bash
wget https://download.geofabrik.de/asia/india-latest.osm.pbf
```

---

## 3. Clip to your area of interest

Bangalore center: **12.9716°N, 77.5946°E**

```bash
osmium extract -b 76.6,12.0,78.6,13.9 india-latest.osm.pbf -o bangalore-100km.osm.pbf

# Sanity check — confirms the file is valid and non-empty
osmium fileinfo bangalore-100km.osm.pbf
```

Bounding box format: `min_longitude,min_latitude,max_longitude,max_latitude`.
Widen or narrow the numbers to cover a smaller/larger area.

---

## 4. Create the Docker volume and import the data

```bash
docker volume create osm-data

docker run -v /home/rajesh/bangalore-100km.osm.pbf:/data/region.osm.pbf \
  -v osm-data:/data/database/ \
  overv/openstreetmap-tile-server import
```

Wait for the import to finish with `exit 0` and no errors. This builds the
PostGIS database inside the `osm-data` volume.

> **If you need to re-import** (new region, fresh data), always fully wipe the
> volume first to avoid stale-data conflicts:
> ```bash
> docker stop <container>
> docker rm <container>
> docker volume rm osm-data
> docker volume create osm-data
> ```

---

## 5. Start the tile server

```bash
docker run -p 8080:80 -v osm-data:/data/database/ -d --name osm-tiles \
  overv/openstreetmap-tile-server run
```

View the map at: **http://localhost:8080**

---

## 6. Add a REST API layer (PostgREST)

```bash
docker network create osmnet
docker network connect osmnet osm-tiles

docker run -d --name postgrest --network osmnet -p 3001:3000 \
  -e PGRST_DB_URI="postgres://renderer:renderer@osm-tiles:5432/gis" \
  -e PGRST_DB_ANON_ROLE="renderer" \
  postgrest/postgrest
```

Query examples:

```bash
# All points with any amenity tag
curl "http://localhost:3001/planet_osm_point?amenity=not.is.null&limit=10"

# All restaurants
curl "http://localhost:3001/planet_osm_point?amenity=eq.restaurant"

# Search by name (partial match)
curl "http://localhost:3001/planet_osm_point?name=ilike.*HMT*"
```

---

## 7. Make it persistent across reboots

```bash
docker update --restart unless-stopped osm-tiles
docker update --restart unless-stopped postgrest
```

Without this, containers stay stopped after a reboot and need to be started
manually:

```bash
docker start osm-tiles
docker start postgrest
```

---

## Useful direct SQL queries

Connect to the database directly (bypasses password auth issues via peer auth):

```bash
docker exec -it -u postgres osm-tiles psql -d gis
```

Find eateries near a point (e.g. HSR Layout, 12.9121°N 77.6446°E) within 2km:

```sql
SELECT name, amenity, tags->'cuisine' AS cuisine,
  ST_X(ST_Transform(way, 4326)) AS lon,
  ST_Y(ST_Transform(way, 4326)) AS lat
FROM planet_osm_point
WHERE amenity IN ('restaurant', 'cafe', 'fast_food', 'food_court')
AND ST_DWithin(
  way,
  ST_Transform(ST_SetSRID(ST_MakePoint(77.6446, 12.9121), 4326), 3857),
  2000
);
```

> **Note:** columns like `cuisine` aren't stored as their own column by default —
> extra tags live in the `tags` hstore field, accessed with `tags->'key'`.

---

## Troubleshooting notes from this setup

| Symptom | Cause | Fix |
|---|---|---|
| `Conflicts: containerd` on apt install | Mixing `docker.io` and Docker's official repo | Use `docker-ce` packages only |
| `/data/region.osm.pbf: Is a directory` | Bad/relative path passed to `-v` mount | Use a verified absolute path |
| Import falls back to importing Luxembourg | No valid file found at the mount path | Same as above — fix the mount path |
| `role "renderer" already exists` → import fails | Re-running import against a non-empty volume | Fully remove and recreate the `osm-data` volume before re-importing |
| `Peer authentication failed` in psql | Connecting via socket as wrong OS user | Use `docker exec -it -u postgres <container> psql -d gis` |
| `password authentication failed` for renderer over TCP | Password/auth mismatch | Use the postgres superuser via peer auth instead |
| `conflicting options: port publishing and network mode` | Used `--network container:X` with `-p` | Use a named Docker network (`docker network create`) instead |
