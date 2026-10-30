fn main() {
    let mut args = std::env::args().skip(1);
    let mut out_dir = String::from(".");
    let mut input = None;
    let mut target = "web".to_string();
    while let Some(a) = args.next() {
        match a.as_str() {
            "--out-dir" => out_dir = args.next().expect("--out-dir value"),
            "--target" => target = args.next().expect("--target value"),
            "--no-typescript" | "--weak-refs" | "--reference-types" => {}
            s if !s.starts_with('-') => input = Some(s.to_string()),
            _ => {}
        }
    }
    let input = input.expect("missing input .wasm path");
    let mut b = wasm_bindgen_cli_support::Bindgen::new();
    b.input_path(&input);
    match target.as_str() {
        "web" => { b.web(true).unwrap(); }
        "bundler" => { b.bundler(true).unwrap(); }
        "nodejs" => { b.nodejs(true).unwrap(); }
        "no-modules" => { b.no_modules(true).unwrap(); }
        _ => { b.web(true).unwrap(); }
    }
    b.generate(&out_dir).expect("wasm-bindgen generate failed");
}
