-- Aktifkan UUID
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Hapus subscription & tabel lama jika ada
DROP SUBSCRIPTION IF EXISTS my_subscription;
DROP TABLE IF EXISTS pasien;

-- Buat tabel pasien (struktur identik dengan master)
CREATE TABLE pasien (
    pasien_id UUID NOT NULL,
    nama_lengkap VARCHAR(255) NOT NULL,
    tanggal_lahir DATE NOT NULL,
    jenis_kelamin VARCHAR(10),
    alamat TEXT,
    nomor_telepon VARCHAR(20),
    email VARCHAR(255),
    PRIMARY KEY (pasien_id)
);

-- Buat subscription ke pg_master
CREATE SUBSCRIPTION my_subscription
CONNECTION 'host=pg_master port=5432 user=user password=password dbname=hospital_db'
PUBLICATION my_publication
WITH (copy_data = true);
