use sha2::Digest;

pub const SUMS: &str = "SHA256SUMS";
pub const SIGNATURE: &str = "SHA256SUMS.sig";
pub const NIGHTLY: &str = "nightly";
pub type PublicKey = [u8; 32];

pub fn trusted_keys() -> Vec<PublicKey> { Vec::new() }
pub fn parse_keys(_text: &str) -> Result<Vec<PublicKey>, String> { Ok(Vec::new()) }
pub fn message(tag: &str, sums: &[u8]) -> Vec<u8> {
    let mut m = format!("horizon release {tag}\n").into_bytes();
    m.extend_from_slice(sums);
    m
}
pub fn sums_file(files: &[(String, String)]) -> String {
    files.iter().map(|(n, h)| format!("{h}  {n}\n")).collect()
}
pub fn parse_sums(_text: &str) -> Result<Vec<(String, String)>, String> { Ok(Vec::new()) }
pub fn verify(_tag: &str, _sums: &[u8], _sig: &str, _keys: &[PublicKey]) -> Result<Vec<(String, String)>, String> {
    Err("Online release downloads are disabled in web build.".into())
}
pub fn is_nightly(tag: &str) -> bool { tag == NIGHTLY || tag.starts_with("nightly-") }
pub fn nightly_version(title: &str, _published: &str) -> String { title.to_string() }
pub fn nightly_date(_version: &str) -> Option<&str> { None }
pub fn signed_digest(_tag: &str, _asset: &str, _sums: Option<&[u8]>, _sig: Option<&str>, _keys: &[PublicKey]) -> Result<String, String> {
    Err("Online release downloads are disabled in web build.".into())
}
pub fn nightly_digest(_asset: &str, _sums: Option<&[u8]>, _sig: Option<&str>, _keys: &[PublicKey]) -> Result<String, String> {
    Err("Online release downloads are disabled in web build.".into())
}
pub fn legacy_digest(_tag: &str, _asset: &str) -> Option<&'static str> { None }
pub fn check(asset: &str, expected: &str, got: &str) -> Result<(), String> {
    if expected.eq_ignore_ascii_case(got) { Ok(()) } else { Err(format!("{asset} checksum mismatch")) }
}

#[derive(Default)]
pub struct Sha256(sha2::Sha256);
impl Sha256 {
    pub fn update(&mut self, bytes: &[u8]) { self.0.update(bytes); }
    pub fn hex(self) -> String {
        self.0.finalize().iter().map(|b| format!("{b:02x}")).collect()
    }
}
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::default();
    h.update(bytes);
    h.hex()
}
