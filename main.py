from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
from pymongo import MongoClient
import redis
from cassandra.cluster import Cluster, ConsistencyLevel
from neo4j import GraphDatabase
import os
import uuid
from datetime import datetime

app = FastAPI(title="Hospital Management System API")

# --- Database Connections ---
# PostgreSQL
PG_MASTER_HOST = os.getenv("PG_MASTER_HOST", "localhost")
PG_SLAVE_HOST = os.getenv("PG_SLAVE_HOST", "localhost")
PG_USER = os.getenv("PG_USER", "user")
PG_PASSWORD = os.getenv("PG_PASSWORD", "password")
PG_DB = os.getenv("PG_DB", "hospital_db")

# Read from slave for better load distribution, write to master
pg_master_engine = create_engine(f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_MASTER_HOST}:5432/{PG_DB}")
pg_slave_engine = create_engine(f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_SLAVE_HOST}:5432/{PG_DB}")

# MongoDB
MONGO_HOST = os.getenv("MONGO_HOST", "localhost")
mongo_client = MongoClient(f"mongodb://{MONGO_HOST}:27017/")
mongo_db = mongo_client["hospital_db_mongo"]

# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
redis_client = redis.Redis(host=REDIS_HOST, port=6379, db=0)

# Cassandra
CASSANDRA_HOSTS = os.getenv("CASSANDRA_HOSTS", "localhost").split(',')
cassandra_cluster = Cluster(CASSANDRA_HOSTS)
cassandra_session = None # Will be initialized on startup

# Neo4j
NEO4J_URI = f"bolt://{os.getenv('NEO4J_HOST', 'localhost')}:7687"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
neo4j_driver = None # Will be initialized on startup

@app.on_event("startup")
async def startup_event():
    # Initialize Cassandra session
    global cassandra_session
    try:
        cassandra_session = cassandra_cluster.connect()
        # Create keyspace and table if they don't exist
        cassandra_session.execute("""
            CREATE KEYSPACE IF NOT EXISTS hospital_keyspace
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
        """)
        cassandra_session.set_keyspace('hospital_keyspace')
        cassandra_session.execute("""
            CREATE TABLE IF NOT EXISTS patient_activity_log (
                pasien_id UUID,
                timestamp TIMESTAMP,
                activity_type TEXT,
                description TEXT,
                PRIMARY KEY (pasien_id, timestamp)
            ) WITH CLUSTERING ORDER BY (timestamp DESC);
        """)
        print("Cassandra connected and keyspace/table checked.")
    except Exception as e:
        print(f"Error connecting to Cassandra: {e}")
        # Optionally exit or handle more gracefully

    # Initialize Neo4j driver
    global neo4j_driver
    try:
        neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        neo4j_driver.verify_connectivity()
        print("Neo4j connected.")
    except Exception as e:
        print(f"Error connecting to Neo4j: {e}")
        # Optionally exit or handle more gracefully

@app.on_event("shutdown")
async def shutdown_event():
    if cassandra_cluster:
        cassandra_cluster.shutdown()
    if neo4j_driver:
        neo4j_driver.close()
    if mongo_client:
        mongo_client.close()
    if pg_master_engine:
        pg_master_engine.dispose()
    if pg_slave_engine:
        pg_slave_engine.dispose()


# --- PostgreSQL Endpoints ---
@app.post("/pasien/")
async def create_pasien(
    nama_lengkap: str,
    tanggal_lahir: str,
    jenis_kelamin: str,
    alamat: str,
    nomor_telepon: str,
    email: str
):
    try:
        with pg_master_engine.connect() as connection:
            result = connection.execute(
                text(
                    """
                    INSERT INTO Pasien (nama_lengkap, tanggal_lahir, jenis_kelamin, alamat, nomor_telepon, email)
                    VALUES (:nama_lengkap, :tanggal_lahir, :jenis_kelamin, :alamat, :nomor_telepon, :email)
                    RETURNING pasien_id
                    """
                ),
                {
                    "nama_lengkap": nama_lengkap,
                    "tanggal_lahir": tanggal_lahir,
                    "jenis_kelamin": jenis_kelamin,
                    "alamat": alamat,
                    "nomor_telepon": nomor_telepon,
                    "email": email,
                },
            )
            connection.commit()
            pasien_id = result.scalar_one()
            return {"message": "Pasien created successfully", "pasien_id": pasien_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pasien/{pasien_id}")
async def get_pasien(pasien_id: str):
    try:
        with pg_slave_engine.connect() as connection:
            result = connection.execute(
                text("SELECT * FROM Pasien WHERE pasien_id = :pid"),
                {"pid": pasien_id}
            ).fetchone()
            if result:
                return dict(result._mapping)
            raise HTTPException(status_code=404, detail="404: Pasien not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- MongoDB Endpoints ---
@app.post("/rekam_medis/")
async def create_rekam_medis(
    pasien_id: str,
    jenis_rekam: str,
    data_detail: dict,
    catatan_tambahan: str = None
):
    try:
        rekam_medis_data = {
            "pasien_id": pasien_id,
            "tanggal_rekam": datetime.now(),
            "jenis_rekam": jenis_rekam,
            "data_detail": data_detail,
            "catatan_tambahan": catatan_tambahan,
        }
        result = mongo_db.RekamMedis.insert_one(rekam_medis_data)
        return {"message": "Rekam medis created successfully", "rekam_medis_id": str(result.inserted_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/rekam_medis/{pasien_id}/latest")
async def get_latest_rekam_medis(pasien_id: str):
    try:
        rekam_medis = mongo_db.RekamMedis.find_one(
            {"pasien_id": pasien_id},
            sort=[("tanggal_rekam", -1)]
        )
        if rekam_medis:
            rekam_medis["_id"] = str(rekam_medis["_id"]) # Convert ObjectId to string
            return rekam_medis
        raise HTTPException(status_code=404, detail="Rekam medis not found for this patient")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Redis Endpoints ---
@app.post("/redis/set_appointment_status/{janji_temu_id}")
async def create_janji_temu(
    pasien_id: str,
    dokter_id: str,
    tanggal_waktu: str,  # Format YYYY-MM-DD HH:MM:SS
    status_janji: str = "Terjadwal",
    catatan: str = None
):
    try:
        parsed_tanggal_waktu = datetime.strptime(tanggal_waktu, "%Y-%m-%d %H:%M:%S")

        with pg_master_engine.connect() as connection:
            result = connection.execute(
                text("""
                    INSERT INTO JanjiTemu (pasien_id, dokter_id, tanggal_waktu, status_janji, catatan)
                    VALUES (:pasien_id, :dokter_id, :tanggal_waktu, :status_janji, :catatan)
                    RETURNING janji_temu_id
                """),
                {
                    "pasien_id": pasien_id,
                    "dokter_id": dokter_id,
                    "tanggal_waktu": parsed_tanggal_waktu,
                    "status_janji": status_janji,
                    "catatan": catatan,
                },
            )
            connection.commit()
            janji_temu_id = result.scalar_one()

            # ✅ Tambahkan status janji temu ke Redis
            redis_client.set(f"appointment:status:{janji_temu_id}", status_janji)

            return {
                "message": "Janji temu created successfully",
                "janji_temu_id": janji_temu_id
            }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=f"Invalid date/time format. Use YYYY-MM-DD HH:MM:SS. Error: {ve}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/redis/get_appointment_status/{janji_temu_id}")
async def get_appointment_status(janji_temu_id: str):
    try:
        status = redis_client.get(f"appointment:status:{janji_temu_id}")
        if status:
            return {"janji_temu_id": janji_temu_id, "status": status.decode('utf-8')}
        raise HTTPException(status_code=404, detail="Appointment status not found in cache")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Cassandra Endpoints ---
from cassandra.query import SimpleStatement

@app.post("/log_activity/")
async def log_activity(pasien_id: str, activity_type: str, description: str):
    try:
        if not cassandra_session:
            raise HTTPException(status_code=500, detail="Cassandra session not initialized.")
        pasien_uuid = uuid.UUID(pasien_id)  # Pastikan UUID valid
        timestamp = datetime.now()
        
        statement = SimpleStatement(
            """
            INSERT INTO patient_activity_log (pasien_id, timestamp, activity_type, description)
            VALUES (%s, %s, %s, %s)
            """,
            consistency_level=ConsistencyLevel.ONE
        )
        
        cassandra_session.execute(statement, (pasien_uuid, timestamp, activity_type, description))
        return {"message": "Activity logged successfully"}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pasien_id format (must be UUID)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to log activity: {str(e)}")


@app.get("/log_activity/{pasien_id}")
async def get_activity_log(pasien_id: str):
    try:
        if not cassandra_session:
            raise HTTPException(status_code=500, detail="Cassandra session not initialized.")
        pasien_uuid = uuid.UUID(pasien_id)
        rows = cassandra_session.execute(
            """
            SELECT * FROM patient_activity_log WHERE pasien_id = %s
            """,
            (pasien_uuid,)
        )
        logs = []
        for row in rows:
            logs.append({
                "pasien_id": str(row.pasien_id),
                "timestamp": row.timestamp.isoformat(),
                "activity_type": row.activity_type,
                "description": row.description
            })
        return logs
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pasien_id format (must be UUID)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve activity logs: {str(e)}")

# --- Neo4j Endpoints ---
@app.post("/neo4j/create_relationship")
async def create_neo4j_relationship(
    node1_type: str, node1_id: str,
    node2_type: str, node2_id: str,
    relationship_type: str,
    properties: dict = None
):
    try:
        if not neo4j_driver:
            raise HTTPException(status_code=500, detail="Neo4j driver not initialized.")
        with neo4j_driver.session() as session:
            # Cypher query untuk membuat atau menemukan node dan membuat relasi
            query = (
    f"MERGE (a:{node1_type} {{id: $node1_id}}) "
    f"MERGE (b:{node2_type} {{id: $node2_id}}) "
    f"MERGE (a)-[r:{relationship_type}]->(b) "
    "SET r += $properties "
    "RETURN a, r, b"
)

            session.run(query, node1_id=node1_id, node2_id=node2_id, properties=properties or {})
            return {"message": "Relationship created/merged successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Neo4j relationship: {str(e)}")


@app.get("/neo4j/get_patient_relationships/{pasien_id}")
async def get_patient_relationships(pasien_id: str):
    try:
        if not neo4j_driver:
            raise HTTPException(status_code=500, detail="Neo4j driver not initialized.")
        with neo4j_driver.session() as session:
            query = (
    f"MATCH (p {{id: $pasien_id}})-[r]-(o) "
    "RETURN p, r, o"
)
            print("Running Neo4j query with:", pasien_id)
            result = session.run(query, pasien_id=pasien_id)
            relationships = []
            for record in result:
                print("Record:", record)  # Tambahkan ini
                relationships.append({
                    "pasien": record["p"].properties,
                    "relationship_type": record["r"].type,
                    "related_node": record["o"].properties,
                    "relationship_properties": record["r"].properties
                })
            return relationships
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Neo4j relationships: {str(e)}")


# --- Skenario Kueri Terdistribusi ---
@app.get("/distributed_query/pasien_summary/{pasien_id}")
async def get_pasien_summary(pasien_id: str):
    pasien_info = {}
    rekam_medis_terakhir = {}
    status_janji_temu = "Tidak ada janji temu mendatang (dari Redis)"
    log_aktivitas = []
    neo4j_relationships = []

    # 1. PostgreSQL (Pasien)
    try:
        with pg_slave_engine.connect() as connection:
            result = connection.execute(
                text("SELECT pasien_id, nama_lengkap, tanggal_lahir, email FROM Pasien WHERE pasien_id = :id"),
                {"id": pasien_id}
            ).fetchone()
            if result:
                pasien_info = dict(result._mapping)
            else:
                raise HTTPException(status_code=404, detail="Pasien not found in PostgreSQL")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting patient from PG: {str(e)}")

    # 2. MongoDB
    try:
        rekam_medis = mongo_db.RekamMedis.find_one(
            {"pasien_id": pasien_id},
            sort=[("tanggal_rekam", -1)]
        )
        if rekam_medis:
            rekam_medis["_id"] = str(rekam_medis["_id"])
            rekam_medis_terakhir = rekam_medis
    except Exception as e:
        print(f"Warning MongoDB: {e}")

    # 3. Redis
    try:
        dummy_janji_temu_id = f"dummy_appt_for_patient_{pasien_id}"
        redis_status = redis_client.get(f"appointment:status:{dummy_janji_temu_id}")
        if redis_status:
            status_janji_temu = f"Dummy status from Redis: {redis_status.decode('utf-8')}"
    except Exception as e:
        print(f"Warning Redis: {e}")

    # 4. Cassandra
    try:
        if cassandra_session:
            pasien_uuid_obj = uuid.UUID(pasien_id)
            rows = cassandra_session.execute(
                "SELECT timestamp, activity_type, description FROM patient_activity_log WHERE pasien_id = %s",
                (pasien_uuid_obj,)
            )
            for row in rows:
                log_aktivitas.append({
                    "timestamp": row.timestamp.isoformat(),
                    "activity_type": row.activity_type,
                    "description": row.description
                })
    except Exception as e:
        print(f"Warning Cassandra: {e}")

    # 5. Neo4j
    try:
        if neo4j_driver:
            with neo4j_driver.session() as session:
                query = (
                    "MATCH (p:Pasien {id: $pasien_id})-[r]-(o) "
                    "RETURN p, r, o"
                )
                result = session.run(query, pasien_id=pasien_id)
                for record in result:
                    neo4j_relationships.append({
                        "node_type": record["o"].labels[0] if record["o"].labels else "Unknown",
                        "related_id": record["o"].get("id"),
                        "relationship_type": record["r"].type,
                        "relationship_properties": record["r"]._properties
                    })
    except Exception as e:
        print(f"Warning Neo4j: {e}")

    return {
        "pasien_info": pasien_info,
        "rekam_medis_terakhir": rekam_medis_terakhir,
        "status_janji_temu_redis": status_janji_temu,
        "log_aktivitas_cassandra": log_aktivitas,
        "neo4j_relationships": neo4j_relationships
    }


# --- Implementasi FDW (Foreign Data Wrapper) ---
# FDW setup cannot be part of docker-entrypoint-initdb.d
# It needs to be executed after both PostgreSQL and Cassandra are up and running.
# You will execute these commands manually via pgAdmin or psql to pg_master.
# Make sure postgresql-contrib is installed in your Docker image
# (or use a base image that includes it, postgres:15 usually does).

# 1. Install extension (on pg_master)
# CREATE EXTENSION IF NOT EXISTS postgres_fdw;

# 2. Create server for Cassandra (on pg_master)
# Replace 'cassandra' with the actual Cassandra service name from docker-compose
# CREATE SERVER cassandra_server
#     FOREIGN DATA WRAPPER postgres_fdw
#     OPTIONS (host 'cassandra', port '9042', dbname 'hospital_keyspace'); # dbname for Cassandra is keyspace

# 3. Create user mapping (on pg_master)
# CREATE USER MAPPING FOR user
#     SERVER cassandra_server
#     OPTIONS (username 'cassandra', password 'cassandra'); # Cassandra usually doesn't need auth by default in docker

# 4. Create foreign table (on pg_master)
# This assumes your Cassandra table is named patient_activity_log and has these columns.
# Ensure column types match!
# CREATE FOREIGN TABLE cassandra_patient_activity_log (
#     pasien_id UUID,
#     timestamp TIMESTAMP,
#     activity_type TEXT,
#     description TEXT
# )
# SERVER cassandra_server
# OPTIONS (schema_name 'public', table_name 'patient_activity_log'); # schema_name and table_name for Cassandra


# Now you can query cassandra_patient_activity_log directly from pg_master:
# SELECT * FROM cassandra_patient_activity_log WHERE pasien_id = 'YOUR_PATIENT_UUID';