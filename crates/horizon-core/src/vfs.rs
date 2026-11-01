//! Virtual filesystem abstraction: delegates to `std::fs` on native targets and
//! provides an in-memory + gzip-pack + IndexedDB-syncable filesystem on `wasm32`.

use std::io;
use std::path::{Path, PathBuf};

pub const PACK_MAGIC: &[u8; 8] = b"HZVFS001";
pub const PACK_FLAG_RAW: u8 = 0;
pub const PACK_FLAG_GZIP: u8 = 1;
pub const PACK_FLAG_DIR: u8 = 2;

pub trait VfsPathExt {
    fn vfs_exists(&self) -> bool;
    fn vfs_is_file(&self) -> bool;
    fn vfs_is_dir(&self) -> bool;
    fn vfs_canonicalize(&self) -> io::Result<PathBuf>;
}

impl VfsPathExt for Path {
    #[inline]
    fn vfs_exists(&self) -> bool {
        exists(self)
    }
    #[inline]
    fn vfs_is_file(&self) -> bool {
        is_file(self)
    }
    #[inline]
    fn vfs_is_dir(&self) -> bool {
        is_dir(self)
    }
    #[inline]
    fn vfs_canonicalize(&self) -> io::Result<PathBuf> {
        canonicalize(self)
    }
}

impl VfsPathExt for PathBuf {
    #[inline]
    fn vfs_exists(&self) -> bool {
        exists(self)
    }
    #[inline]
    fn vfs_is_file(&self) -> bool {
        is_file(self)
    }
    #[inline]
    fn vfs_is_dir(&self) -> bool {
        is_dir(self)
    }
    #[inline]
    fn vfs_canonicalize(&self) -> io::Result<PathBuf> {
        canonicalize(self)
    }
}

pub trait VfsTypeExt {
    fn vfs_is_dir(&self) -> bool;
    fn vfs_is_file(&self) -> bool;
}

impl VfsTypeExt for Metadata {
    #[inline]
    fn vfs_is_dir(&self) -> bool {
        self.is_dir()
    }
    #[inline]
    fn vfs_is_file(&self) -> bool {
        self.is_file()
    }
}

impl VfsTypeExt for FileType {
    #[inline]
    fn vfs_is_dir(&self) -> bool {
        self.is_dir()
    }
    #[inline]
    fn vfs_is_file(&self) -> bool {
        self.is_file()
    }
}

// ============================================================================
// Native implementation (`not(target_arch = "wasm32")`)
// ============================================================================
#[cfg(not(target_arch = "wasm32"))]
mod imp {
    use super::*;

    pub use std::fs::{
        DirEntry, File, FileType, Metadata, OpenOptions, ReadDir, TryLockError, canonicalize, copy,
        create_dir, create_dir_all, hard_link, metadata, read, read_dir, read_link, read_to_string,
        remove_dir, remove_dir_all, remove_file, rename, symlink_metadata, write,
    };

    #[inline]
    pub fn exists(path: impl AsRef<Path>) -> bool {
        path.as_ref().exists()
    }

    #[inline]
    pub fn is_file(path: impl AsRef<Path>) -> bool {
        path.as_ref().is_file()
    }

    #[inline]
    pub fn is_dir(path: impl AsRef<Path>) -> bool {
        path.as_ref().is_dir()
    }

    #[inline]
    pub fn current_dir() -> io::Result<PathBuf> {
        std::env::current_dir()
    }

    /// `std::env::temp_dir`: scratch space for files that only need to survive
    /// the run that made them.
    #[inline]
    pub fn temp_dir() -> PathBuf {
        std::env::temp_dir()
    }

    /// `std::env::home_dir`. `None` where the platform has no home folder.
    #[inline]
    pub fn home_dir() -> Option<PathBuf> {
        std::env::home_dir()
    }

    /// `std::env::current_exe`: the running game binary.
    #[inline]
    pub fn current_exe() -> io::Result<PathBuf> {
        std::env::current_exe()
    }
}

// ============================================================================
// WebAssembly implementation (`target_arch = "wasm32"`)
/// `std::path::absolute`: a path made absolute against the current folder.
/// `std::path::absolute` itself panics on `wasm32`, so callers go through this.
#[cfg(not(target_arch = "wasm32"))]
pub fn absolute(path: impl AsRef<Path>) -> io::Result<PathBuf> {
    std::path::absolute(path)
}

/// `std::path::absolute`: a path made absolute against the current folder.
#[cfg(target_arch = "wasm32")]
pub fn absolute(path: impl AsRef<Path>) -> io::Result<PathBuf> {
    let path = path.as_ref();
    if path.is_absolute() {
        return Ok(path.to_path_buf());
    }
    Ok(current_dir()?.join(path))
}

// ============================================================================
#[cfg(target_arch = "wasm32")]
mod imp {
    use super::*;
    use crate::time::{Duration, SystemTime, UNIX_EPOCH};
    use flate2::read::GzDecoder;
    use std::collections::{BTreeMap, BTreeSet};
    use std::ffi::OsString;
    use std::io::{Cursor, Read, Seek, SeekFrom, Write};
    use std::path::Component;
    use std::sync::{Arc, Mutex, OnceLock, RwLock};

    const DEFAULT_CWD: &str = "/game";

    #[derive(Clone)]
    enum Entry {
        PackRaw {
            pack: Arc<[u8]>,
            offset: usize,
            len: usize,
            mtime_secs: u64,
        },
        PackGzip {
            pack: Arc<[u8]>,
            offset: usize,
            comp_len: usize,
            raw_len: u64,
            mtime_secs: u64,
        },
        Memory {
            bytes: Arc<Vec<u8>>,
            mtime_secs: u64,
        },
    }

    impl Entry {
        fn len(&self) -> u64 {
            match self {
                Entry::PackRaw { len, .. } => *len as u64,
                Entry::PackGzip { raw_len, .. } => *raw_len,
                Entry::Memory { bytes, .. } => bytes.len() as u64,
            }
        }

        fn mtime_secs(&self) -> u64 {
            match self {
                Entry::PackRaw { mtime_secs, .. }
                | Entry::PackGzip { mtime_secs, .. }
                | Entry::Memory { mtime_secs, .. } => *mtime_secs,
            }
        }

        fn read_bytes(&self) -> io::Result<Vec<u8>> {
            match self {
                Entry::PackRaw {
                    pack, offset, len, ..
                } => Ok(pack[*offset..*offset + *len].to_vec()),
                Entry::PackGzip {
                    pack,
                    offset,
                    comp_len,
                    raw_len,
                    ..
                } => {
                    let slice = &pack[*offset..*offset + *comp_len];
                    let mut decoder = GzDecoder::new(slice);
                    let mut out = Vec::with_capacity(*raw_len as usize);
                    decoder.read_to_end(&mut out)?;
                    Ok(out)
                }
                Entry::Memory { bytes, .. } => Ok((**bytes).clone()),
            }
        }
    }

    struct MemFs {
        cwd: String,
        dirs: BTreeSet<String>,
        files: BTreeMap<String, Entry>,
        dirty_files: BTreeSet<String>,
        deleted_files: BTreeSet<String>,
    }

    impl MemFs {
        fn new() -> Self {
            let mut dirs = BTreeSet::new();
            dirs.insert("/".to_owned());
            dirs.insert("/game".to_owned());
            dirs.insert("/game/saves".to_owned());
            dirs.insert("/game/.horizon-cache".to_owned());
            dirs.insert("/resources".to_owned());
            Self {
                cwd: DEFAULT_CWD.to_owned(),
                dirs,
                files: BTreeMap::new(),
                dirty_files: BTreeSet::new(),
                deleted_files: BTreeSet::new(),
            }
        }

        fn normalize(&self, path: &Path) -> String {
            let raw = path.to_string_lossy();
            let joined = if raw.starts_with('/') {
                PathBuf::from(raw.as_ref())
            } else {
                PathBuf::from(&self.cwd).join(path)
            };
            let mut parts: Vec<String> = Vec::new();
            for comp in joined.components() {
                match comp {
                    Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
                    Component::ParentDir => {
                        parts.pop();
                    }
                    Component::Normal(s) => {
                        parts.push(s.to_string_lossy().into_owned());
                    }
                }
            }
            if parts.is_empty() {
                "/".to_owned()
            } else {
                format!("/{}", parts.join("/"))
            }
        }

        fn ensure_parent_dirs(&mut self, norm: &str) {
            let mut cur = norm;
            while let Some((parent, _)) = cur.rsplit_once('/') {
                if parent.is_empty() {
                    self.dirs.insert("/".to_owned());
                    break;
                }
                self.dirs.insert(parent.to_owned());
                cur = parent;
            }
        }

        fn mark_modified(&mut self, norm: &str) {
            if norm.starts_with("/game/") && !norm.starts_with("/game/.horizon-cache/") {
                self.deleted_files.remove(norm);
                self.dirty_files.insert(norm.to_owned());
            }
        }

        fn mark_deleted(&mut self, norm: &str) {
            if norm.starts_with("/game/") && !norm.starts_with("/game/.horizon-cache/") {
                self.dirty_files.remove(norm);
                self.deleted_files.insert(norm.to_owned());
            }
        }
    }

    fn state() -> &'static RwLock<MemFs> {
        static FS: OnceLock<RwLock<MemFs>> = OnceLock::new();
        FS.get_or_init(|| RwLock::new(MemFs::new()))
    }

    fn now_secs() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(1_730_000_000, |d| d.as_secs())
    }

    fn not_found(msg: &str) -> io::Error {
        io::Error::new(io::ErrorKind::NotFound, msg.to_owned())
    }

    fn already_exists(msg: &str) -> io::Error {
        io::Error::new(io::ErrorKind::AlreadyExists, msg.to_owned())
    }

    pub fn mount_pack(bytes: Vec<u8>) -> io::Result<usize> {
        if bytes.len() < 12 || &bytes[..8] != PACK_MAGIC {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid HZVFS001 pack header",
            ));
        }
        let count = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
        let pack: Arc<[u8]> = Arc::from(bytes);
        let mut pos = 12usize;
        let mut fs = crate::sync::write(state());
        for _ in 0..count {
            if pos + 2 > pack.len() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "truncated pack path len",
                ));
            }
            let path_len = u16::from_le_bytes(pack[pos..pos + 2].try_into().unwrap()) as usize;
            pos += 2;
            if pos + path_len + 33 > pack.len() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "truncated pack entry",
                ));
            }
            let path_str = std::str::from_utf8(&pack[pos..pos + path_len])
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            pos += path_len;
            let flags = pack[pos];
            pos += 1;
            let raw_len = u64::from_le_bytes(pack[pos..pos + 8].try_into().unwrap());
            pos += 8;
            let mtime_secs = u64::from_le_bytes(pack[pos..pos + 8].try_into().unwrap());
            pos += 8;
            let data_offset = u64::from_le_bytes(pack[pos..pos + 8].try_into().unwrap()) as usize;
            pos += 8;
            let data_len = u64::from_le_bytes(pack[pos..pos + 8].try_into().unwrap()) as usize;
            pos += 8;

            let norm = fs.normalize(Path::new(path_str));
            fs.ensure_parent_dirs(&norm);
            match flags {
                PACK_FLAG_DIR => {
                    fs.dirs.insert(norm);
                }
                PACK_FLAG_RAW => {
                    if data_offset + data_len > pack.len() {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "pack raw slice out of bounds",
                        ));
                    }
                    fs.files.insert(
                        norm,
                        Entry::PackRaw {
                            pack: pack.clone(),
                            offset: data_offset,
                            len: data_len,
                            mtime_secs,
                        },
                    );
                }
                PACK_FLAG_GZIP => {
                    if data_offset + data_len > pack.len() {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "pack gzip slice out of bounds",
                        ));
                    }
                    fs.files.insert(
                        norm,
                        Entry::PackGzip {
                            pack: pack.clone(),
                            offset: data_offset,
                            comp_len: data_len,
                            raw_len,
                            mtime_secs,
                        },
                    );
                }
                other => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("unknown pack flag {other}"),
                    ));
                }
            }
        }
        Ok(count)
    }

    pub fn import_persisted_file(path: &str, data: Vec<u8>, mtime_secs: u64) {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(Path::new(path));
        fs.ensure_parent_dirs(&norm);
        fs.dirs.remove(&norm);
        fs.files.insert(
            norm,
            Entry::Memory {
                bytes: Arc::new(data),
                mtime_secs,
            },
        );
    }

    /// How many deletions one drain reports: the paths alone are small, but a
    /// world that deletes a tree of files should not hand them all over at once
    /// either.
    const DELETIONS_PER_DRAIN: usize = 256;

    /// The files changed under `/game` that the page has not stored yet, at
    /// most about `budget` bytes of their contents: `(written, deleted, more)`.
    ///
    /// A save marks every chunk it wrote dirty in one go, and the region files
    /// are hundreds of kilobytes each: copying all of them while holding the
    /// lock is a hitch the page's frame pays for and cannot avoid. So each file
    /// is taken out of the dirty set under the lock and copied without it, and
    /// one call stops at the budget; `more` says another call has work left. A
    /// file written again while its bytes are copied is marked dirty again, so
    /// the next call reports it.
    pub fn drain_persisted_changes(
        budget: usize,
    ) -> (Vec<(String, Vec<u8>, u64)>, Vec<String>, bool) {
        let mut written = Vec::new();
        let mut used = 0usize;
        loop {
            let next = {
                let mut fs = crate::sync::write(state());
                let path = fs.dirty_files.iter().next().cloned();
                if let Some(path) = &path {
                    fs.dirty_files.remove(path);
                }
                path
            };
            let Some(path) = next else { break };
            // Taken out of the map by value: the copy (a region file, a
            // compressed resource unpacked) happens with the lock released.
            let entry = {
                let fs = crate::sync::read(state());
                fs.files.get(&path).cloned()
            };
            if let Some(entry) = entry
                && let Ok(bytes) = entry.read_bytes()
            {
                used += bytes.len();
                written.push((path, bytes, entry.mtime_secs()));
            }
            if used >= budget {
                break;
            }
        }
        let deleted = {
            let mut fs = crate::sync::write(state());
            let deleted: Vec<String> = fs
                .deleted_files
                .iter()
                .take(DELETIONS_PER_DRAIN)
                .cloned()
                .collect();
            for path in &deleted {
                fs.deleted_files.remove(path);
            }
            deleted
        };
        let more = {
            let fs = crate::sync::read(state());
            !fs.dirty_files.is_empty() || !fs.deleted_files.is_empty()
        };
        (written, deleted, more)
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct FileType {
        is_dir: bool,
    }

    impl FileType {
        pub fn is_dir(&self) -> bool {
            self.is_dir
        }
        pub fn is_file(&self) -> bool {
            !self.is_dir
        }
        pub fn is_symlink(&self) -> bool {
            false
        }
    }

    #[derive(Clone, Debug)]
    pub struct Metadata {
        is_dir: bool,
        len: u64,
        mtime_secs: u64,
    }

    impl Metadata {
        pub fn is_dir(&self) -> bool {
            self.is_dir
        }
        pub fn is_file(&self) -> bool {
            !self.is_dir
        }
        pub fn is_symlink(&self) -> bool {
            false
        }
        pub fn len(&self) -> u64 {
            self.len
        }
        pub fn is_empty(&self) -> bool {
            self.len == 0
        }
        pub fn file_type(&self) -> FileType {
            FileType {
                is_dir: self.is_dir,
            }
        }
        pub fn modified(&self) -> io::Result<SystemTime> {
            Ok(UNIX_EPOCH + Duration::from_secs(self.mtime_secs))
        }
        pub fn created(&self) -> io::Result<SystemTime> {
            self.modified()
        }
    }

    #[derive(Debug)]
    pub enum TryLockError {
        Error(io::Error),
        WouldBlock,
    }

    #[derive(Clone, Debug)]
    pub struct DirEntry {
        path: PathBuf,
        name: OsString,
        meta: Metadata,
    }

    impl DirEntry {
        pub fn path(&self) -> PathBuf {
            self.path.clone()
        }
        pub fn file_name(&self) -> OsString {
            self.name.clone()
        }
        pub fn metadata(&self) -> io::Result<Metadata> {
            Ok(self.meta.clone())
        }
        pub fn file_type(&self) -> io::Result<FileType> {
            Ok(self.meta.file_type())
        }
    }

    pub struct ReadDir {
        entries: std::vec::IntoIter<io::Result<DirEntry>>,
    }

    impl Iterator for ReadDir {
        type Item = io::Result<DirEntry>;
        fn next(&mut self) -> Option<Self::Item> {
            self.entries.next()
        }
    }

    struct FileInner {
        norm_path: String,
        cursor: Cursor<Vec<u8>>,
        readable: bool,
        writable: bool,
        append: bool,
        dirty: bool,
        mtime_secs: u64,
    }

    impl FileInner {
        fn commit(&mut self) -> io::Result<()> {
            if !self.writable || !self.dirty {
                return Ok(());
            }
            let now = now_secs();
            self.mtime_secs = now;
            let bytes = Arc::new(self.cursor.get_ref().clone());
            let mut fs = crate::sync::write(state());
            fs.ensure_parent_dirs(&self.norm_path);
            fs.files.insert(
                self.norm_path.clone(),
                Entry::Memory {
                    bytes,
                    mtime_secs: now,
                },
            );
            fs.mark_modified(&self.norm_path);
            self.dirty = false;
            Ok(())
        }
    }

    pub struct File {
        inner: Mutex<FileInner>,
    }

    impl File {
        pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
            OpenOptions::new().read(true).open(path)
        }

        pub fn create(path: impl AsRef<Path>) -> io::Result<Self> {
            OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(path)
        }

        pub fn options() -> OpenOptions {
            OpenOptions::new()
        }

        pub fn metadata(&self) -> io::Result<Metadata> {
            let guard = crate::sync::lock(&self.inner);
            Ok(Metadata {
                is_dir: false,
                len: guard.cursor.get_ref().len() as u64,
                mtime_secs: guard.mtime_secs,
            })
        }

        pub fn set_len(&self, size: u64) -> io::Result<()> {
            let mut guard = crate::sync::lock(&self.inner);
            if !guard.writable {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "file not open for writing",
                ));
            }
            guard.cursor.get_mut().resize(size as usize, 0);
            guard.dirty = true;
            guard.commit()
        }

        pub fn sync_all(&self) -> io::Result<()> {
            let mut guard = crate::sync::lock(&self.inner);
            guard.commit()
        }

        pub fn sync_data(&self) -> io::Result<()> {
            self.sync_all()
        }

        pub fn set_modified(&self, time: SystemTime) -> io::Result<()> {
            let mut guard = crate::sync::lock(&self.inner);
            guard.mtime_secs = time
                .duration_since(UNIX_EPOCH)
                .map_or(1_730_000_000, |d| d.as_secs());
            guard.dirty = true;
            guard.commit()
        }

        pub fn lock(&self) -> io::Result<()> {
            Ok(())
        }

        pub fn lock_shared(&self) -> io::Result<()> {
            Ok(())
        }

        pub fn try_lock(&self) -> Result<(), TryLockError> {
            Ok(())
        }

        pub fn try_lock_shared(&self) -> Result<(), TryLockError> {
            Ok(())
        }

        pub fn unlock(&self) -> io::Result<()> {
            Ok(())
        }
    }

    impl Read for File {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let mut guard = crate::sync::lock(&self.inner);
            if !guard.readable {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "file not open for reading",
                ));
            }
            guard.cursor.read(buf)
        }
    }

    impl Write for File {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            let mut guard = crate::sync::lock(&self.inner);
            if !guard.writable {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "file not open for writing",
                ));
            }
            if guard.append {
                let end = guard.cursor.get_ref().len() as u64;
                guard.cursor.set_position(end);
            }
            let n = guard.cursor.write(buf)?;
            guard.dirty = true;
            Ok(n)
        }

        fn flush(&mut self) -> io::Result<()> {
            let mut guard = crate::sync::lock(&self.inner);
            guard.commit()
        }
    }

    impl Seek for File {
        fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
            let mut guard = crate::sync::lock(&self.inner);
            guard.cursor.seek(pos)
        }
    }

    impl Drop for File {
        fn drop(&mut self) {
            let mut guard = crate::sync::lock(&self.inner);
            let _ = guard.commit();
        }
    }

    #[derive(Clone, Debug, Default)]
    pub struct OpenOptions {
        read: bool,
        write: bool,
        append: bool,
        truncate: bool,
        create: bool,
        create_new: bool,
    }

    impl OpenOptions {
        pub fn new() -> Self {
            Self::default()
        }
        pub fn read(&mut self, read: bool) -> &mut Self {
            self.read = read;
            self
        }
        pub fn write(&mut self, write: bool) -> &mut Self {
            self.write = write;
            self
        }
        pub fn append(&mut self, append: bool) -> &mut Self {
            self.append = append;
            if append {
                self.write = true;
            }
            self
        }
        pub fn truncate(&mut self, truncate: bool) -> &mut Self {
            self.truncate = truncate;
            self
        }
        pub fn create(&mut self, create: bool) -> &mut Self {
            self.create = create;
            self
        }
        pub fn create_new(&mut self, create_new: bool) -> &mut Self {
            self.create_new = create_new;
            if create_new {
                self.create = true;
            }
            self
        }

        pub fn open(&self, path: impl AsRef<Path>) -> io::Result<File> {
            let writable = self.write || self.append;
            // The entry is taken out of the map under the lock and its bytes are
            // read after it is dropped: opening a resource can unpack a gzip
            // member, and copying that while holding the filesystem's lock is
            // time the page's frame spends waiting on it (a save running beside
            // it holds the same lock for its own writes).
            let (norm, existing, created) = {
                let mut fs = crate::sync::write(state());
                let norm = fs.normalize(path.as_ref());
                if fs.dirs.contains(&norm) {
                    return Err(io::Error::new(io::ErrorKind::IsADirectory, norm));
                }
                if self.create_new && fs.files.contains_key(&norm) {
                    return Err(already_exists(&norm));
                }
                let existing = fs.files.get(&norm).cloned();
                let mut created = None;
                if existing.is_none() {
                    if !(self.create && writable) {
                        return Err(not_found(&norm));
                    }
                    let now = now_secs();
                    fs.ensure_parent_dirs(&norm);
                    fs.files.insert(
                        norm.clone(),
                        Entry::Memory {
                            bytes: Arc::new(Vec::new()),
                            mtime_secs: now,
                        },
                    );
                    fs.mark_modified(&norm);
                    created = Some(now);
                }
                (norm, existing, created)
            };

            let (initial, mtime_secs, dirty) = match (existing, created) {
                (Some(entry), _) => {
                    if self.truncate && writable {
                        (Vec::new(), now_secs(), true)
                    } else {
                        (entry.read_bytes()?, entry.mtime_secs(), false)
                    }
                }
                (None, Some(now)) => (Vec::new(), now, false),
                (None, None) => (Vec::new(), now_secs(), false),
            };

            let mut file = FileInner {
                norm_path: norm,
                cursor: Cursor::new(initial),
                readable: self.read,
                writable,
                append: self.append,
                dirty,
                mtime_secs,
            };
            if dirty {
                file.commit()?;
            }
            Ok(File {
                inner: Mutex::new(file),
            })
        }
    }

    pub fn exists(path: impl AsRef<Path>) -> bool {
        let fs = crate::sync::read(state());
        let norm = fs.normalize(path.as_ref());
        fs.files.contains_key(&norm) || fs.dirs.contains(&norm)
    }

    pub fn is_file(path: impl AsRef<Path>) -> bool {
        let fs = crate::sync::read(state());
        let norm = fs.normalize(path.as_ref());
        fs.files.contains_key(&norm)
    }

    pub fn is_dir(path: impl AsRef<Path>) -> bool {
        let fs = crate::sync::read(state());
        let norm = fs.normalize(path.as_ref());
        fs.dirs.contains(&norm)
    }

    pub fn current_dir() -> io::Result<PathBuf> {
        let fs = crate::sync::read(state());
        Ok(PathBuf::from(&fs.cwd))
    }

    /// Scratch space inside the in-memory VFS. `std::env::temp_dir` panics on
    /// `wasm32` ("no filesystem on this platform"), so scratch files live under
    /// `/tmp` in the overlay instead; nothing there is persisted.
    pub fn temp_dir() -> PathBuf {
        PathBuf::from("/tmp")
    }

    /// The game's home folder in the web build is the writable `/game` overlay.
    pub fn home_dir() -> Option<PathBuf> {
        Some(PathBuf::from("/game"))
    }

    /// There is no process image to point at in a browser.
    pub fn current_exe() -> io::Result<PathBuf> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "no executable path on the web",
        ))
    }

    pub fn canonicalize(path: impl AsRef<Path>) -> io::Result<PathBuf> {
        let fs = crate::sync::read(state());
        let norm = fs.normalize(path.as_ref());
        if fs.files.contains_key(&norm) || fs.dirs.contains(&norm) {
            Ok(PathBuf::from(norm))
        } else {
            Err(not_found(&norm))
        }
    }

    pub fn metadata(path: impl AsRef<Path>) -> io::Result<Metadata> {
        let fs = crate::sync::read(state());
        let norm = fs.normalize(path.as_ref());
        if let Some(entry) = fs.files.get(&norm) {
            return Ok(Metadata {
                is_dir: false,
                len: entry.len(),
                mtime_secs: entry.mtime_secs(),
            });
        }
        if fs.dirs.contains(&norm) {
            return Ok(Metadata {
                is_dir: true,
                len: 0,
                mtime_secs: 1_700_000_000,
            });
        }
        Err(not_found(&norm))
    }

    pub fn symlink_metadata(path: impl AsRef<Path>) -> io::Result<Metadata> {
        metadata(path)
    }

    pub fn read_link(path: impl AsRef<Path>) -> io::Result<PathBuf> {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("not a symbolic link: {}", path.as_ref().display()),
        ))
    }

    pub fn read(path: impl AsRef<Path>) -> io::Result<Vec<u8>> {
        let entry = {
            let fs = crate::sync::read(state());
            let norm = fs.normalize(path.as_ref());
            fs.files.get(&norm).cloned().ok_or_else(|| not_found(&norm))?
        };
        entry.read_bytes()
    }

    pub fn read_to_string(path: impl AsRef<Path>) -> io::Result<String> {
        let bytes = read(path)?;
        String::from_utf8(bytes).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }

    pub fn write(path: impl AsRef<Path>, contents: impl AsRef<[u8]>) -> io::Result<()> {
        let now = now_secs();
        let data = Arc::new(contents.as_ref().to_vec());
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if fs.dirs.contains(&norm) {
            return Err(io::Error::new(io::ErrorKind::IsADirectory, norm));
        }
        fs.ensure_parent_dirs(&norm);
        fs.files.insert(
            norm.clone(),
            Entry::Memory {
                bytes: data,
                mtime_secs: now,
            },
        );
        fs.mark_modified(&norm);
        Ok(())
    }

    pub fn create_dir(path: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if fs.files.contains_key(&norm) || fs.dirs.contains(&norm) {
            return Err(already_exists(&norm));
        }
        fs.ensure_parent_dirs(&norm);
        fs.dirs.insert(norm);
        Ok(())
    }

    pub fn create_dir_all(path: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if fs.files.contains_key(&norm) {
            return Err(already_exists(&norm));
        }
        fs.ensure_parent_dirs(&norm);
        fs.dirs.insert(norm);
        Ok(())
    }

    pub fn remove_file(path: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if fs.files.remove(&norm).is_some() {
            fs.mark_deleted(&norm);
            Ok(())
        } else {
            Err(not_found(&norm))
        }
    }

    pub fn remove_dir(path: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if !fs.dirs.contains(&norm) {
            return Err(not_found(&norm));
        }
        let prefix = if norm == "/" {
            "/".to_owned()
        } else {
            format!("{norm}/")
        };
        let has_children = fs.files.keys().any(|k| k.starts_with(&prefix))
            || fs.dirs.iter().any(|d| d != &norm && d.starts_with(&prefix));
        if has_children {
            return Err(io::Error::new(io::ErrorKind::DirectoryNotEmpty, norm));
        }
        fs.dirs.remove(&norm);
        Ok(())
    }

    pub fn remove_dir_all(path: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let norm = fs.normalize(path.as_ref());
        if !fs.dirs.contains(&norm) && !fs.files.contains_key(&norm) {
            return Err(not_found(&norm));
        }
        let prefix = format!("{norm}/");
        let files_to_remove: Vec<String> = fs
            .files
            .keys()
            .filter(|k| *k == &norm || k.starts_with(&prefix))
            .cloned()
            .collect();
        for f in files_to_remove {
            fs.files.remove(&f);
            fs.mark_deleted(&f);
        }
        let dirs_to_remove: Vec<String> = fs
            .dirs
            .iter()
            .filter(|d| *d == &norm || d.starts_with(&prefix))
            .cloned()
            .collect();
        for d in dirs_to_remove {
            fs.dirs.remove(&d);
        }
        Ok(())
    }

    pub fn rename(from: impl AsRef<Path>, to: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let src = fs.normalize(from.as_ref());
        let dst = fs.normalize(to.as_ref());
        if src == dst {
            return Ok(());
        }
        if let Some(entry) = fs.files.remove(&src) {
            fs.mark_deleted(&src);
            fs.ensure_parent_dirs(&dst);
            fs.files.insert(dst.clone(), entry);
            fs.mark_modified(&dst);
            return Ok(());
        }
        if fs.dirs.remove(&src) {
            fs.ensure_parent_dirs(&dst);
            fs.dirs.insert(dst.clone());
            let prefix = format!("{src}/");
            let sub_dirs: Vec<String> = fs
                .dirs
                .iter()
                .filter(|d| d.starts_with(&prefix))
                .cloned()
                .collect();
            for d in sub_dirs {
                fs.dirs.remove(&d);
                fs.dirs.insert(format!("{dst}/{}", &d[prefix.len()..]));
            }
            let sub_files: Vec<(String, Entry)> = fs
                .files
                .iter()
                .filter(|(k, _)| k.starts_with(&prefix))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            for (k, v) in sub_files {
                fs.files.remove(&k);
                fs.mark_deleted(&k);
                let nk = format!("{dst}/{}", &k[prefix.len()..]);
                fs.files.insert(nk.clone(), v);
                fs.mark_modified(&nk);
            }
            return Ok(());
        }
        Err(not_found(&src))
    }

    pub fn copy(from: impl AsRef<Path>, to: impl AsRef<Path>) -> io::Result<u64> {
        let bytes = read(&from)?;
        let len = bytes.len() as u64;
        write(to, bytes)?;
        Ok(len)
    }

    pub fn hard_link(original: impl AsRef<Path>, link: impl AsRef<Path>) -> io::Result<()> {
        let mut fs = crate::sync::write(state());
        let src = fs.normalize(original.as_ref());
        let dst = fs.normalize(link.as_ref());
        if fs.files.contains_key(&dst) || fs.dirs.contains(&dst) {
            return Err(already_exists(&dst));
        }
        let entry = fs.files.get(&src).cloned().ok_or_else(|| not_found(&src))?;
        fs.ensure_parent_dirs(&dst);
        fs.files.insert(dst.clone(), entry);
        fs.mark_modified(&dst);
        Ok(())
    }

    pub fn read_dir(path: impl AsRef<Path>) -> io::Result<ReadDir> {
        let base_path = path.as_ref().to_path_buf();
        let fs = crate::sync::read(state());
        let norm = fs.normalize(&base_path);
        if !fs.dirs.contains(&norm) {
            return Err(not_found(&norm));
        }
        let prefix = if norm == "/" {
            "/".to_owned()
        } else {
            format!("{norm}/")
        };

        let mut children: BTreeMap<String, Metadata> = BTreeMap::new();
        for dir in fs.dirs.range(prefix.clone()..) {
            if !dir.starts_with(&prefix) {
                break;
            }
            let rest = &dir[prefix.len()..];
            if !rest.is_empty() && !rest.contains('/') {
                children.insert(
                    rest.to_owned(),
                    Metadata {
                        is_dir: true,
                        len: 0,
                        mtime_secs: 1_700_000_000,
                    },
                );
            }
        }
        for (file, entry) in fs.files.range(prefix.clone()..) {
            if !file.starts_with(&prefix) {
                break;
            }
            let rest = &file[prefix.len()..];
            if !rest.is_empty() && !rest.contains('/') {
                children.insert(
                    rest.to_owned(),
                    Metadata {
                        is_dir: false,
                        len: entry.len(),
                        mtime_secs: entry.mtime_secs(),
                    },
                );
            }
        }

        let out: Vec<io::Result<DirEntry>> = children
            .into_iter()
            .map(|(name, meta)| {
                Ok(DirEntry {
                    path: base_path.join(&name),
                    name: OsString::from(name),
                    meta,
                })
            })
            .collect();
        Ok(ReadDir {
            entries: out.into_iter(),
        })
    }
}

pub use imp::*;
