-- Serpentine accounts. Everything users store is end-to-end encrypted in the browser:
-- the server only ever sees ciphertext, plus a hash of a key derived from the password.
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  auth_hash TEXT NOT NULL,        -- SHA-256(server_salt || auth key derived in the browser)
  auth_salt TEXT NOT NULL,
  wrapped_dek TEXT NOT NULL,      -- data key, encrypted with the password-derived key
  recovery_hash TEXT NOT NULL,
  recovery_salt TEXT NOT NULL,
  recovery_wrapped_dek TEXT NOT NULL, -- data key, encrypted with the recovery key
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS items (
  user_id TEXT NOT NULL,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- project | styles
  meta TEXT NOT NULL,             -- encrypted name/summary (base64)
  size INTEGER NOT NULL,
  chunks INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS chunks (
  user_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  data BLOB NOT NULL,             -- encrypted bytes
  PRIMARY KEY (user_id, item_id, idx)
);
CREATE TABLE IF NOT EXISTS attempts (
  key TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_key ON attempts(key, at);
