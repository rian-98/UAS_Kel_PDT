-- Aktifkan UUID
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Drop tabel jika sudah ada
DROP TABLE IF EXISTS pasien CASCADE;

-- Buat tabel pasien tanpa partisi
CREATE TABLE pasien (
    pasien_id UUID NOT NULL DEFAULT gen_random_uuid(),
    nama_lengkap VARCHAR(255) NOT NULL,
    tanggal_lahir DATE NOT NULL,
    jenis_kelamin VARCHAR(10),
    alamat TEXT,
    nomor_telepon VARCHAR(20),
    email VARCHAR(255),
    PRIMARY KEY (pasien_id)
);

-- Data contoh
INSERT INTO pasien (nama_lengkap, tanggal_lahir, jenis_kelamin, alamat, nomor_telepon, email) VALUES
('Bunga Lestari', '1988-07-20', 'Perempuan', 'Jl. Kenanga 5', '081233334444', 'bunga@email.com'),
('Deni Wijaya', '2005-01-25', 'Laki-laki', 'Jl. Anggrek 12', '081277778888', 'deni@email.com');

-- Hapus publication lama jika ada
DROP PUBLICATION IF EXISTS my_publication;

-- Buat publication baru
CREATE PUBLICATION my_publication FOR TABLE pasien;
