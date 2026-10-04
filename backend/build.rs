//! Embed the complete backend implementation and dependency lock for replay compatibility.
use std::{env,fs,path::{Path,PathBuf}};
fn sources(dir:&Path,out:&mut Vec<PathBuf>){
    for entry in fs::read_dir(dir).expect("backend source directory"){let path=entry.expect("backend source entry").path();
        if path.is_dir(){sources(&path,out)}else if path.extension().is_some_and(|v|v=="rs"||v=="json"||v=="sql"){out.push(path)}
    }
}
fn main(){
    let root=PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());let mut paths=vec![];sources(&root.join("src"),&mut paths);sources(&root.join("assets"),&mut paths);
    if root.join("schema.sql").exists(){paths.push(root.join("schema.sql"));}
    paths.extend([root.join("Cargo.toml"),root.join("Cargo.lock"),root.join("build.rs")]);paths.sort();
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=assets");
    let mut generated="pub const BACKEND_BUILD_INPUTS: &[(&str, &[u8])] = &[\n".to_owned();
    for path in paths{println!("cargo:rerun-if-changed={}",path.display());let relative=path.strip_prefix(&root).unwrap().to_string_lossy();
        generated.push_str(&format!("({:?}, include_bytes!({:?})),\n",relative,path.to_string_lossy()));
    }
    generated.push_str("];\n");
    let mut settings=env::vars().filter(|(key,_)|key.starts_with("CARGO_FEATURE_")||key.starts_with("CARGO_CFG_")||matches!(key.as_str(),"PROFILE"|"OPT_LEVEL"|"DEBUG"|"TARGET")).collect::<Vec<_>>();settings.sort();
    let compiler=std::process::Command::new(env::var_os("RUSTC").unwrap()).arg("--version").output().expect("compiler identity");
    generated.push_str(&format!("pub const BACKEND_BUILD_CONFIG: &str = {:?};\n",format!("{:?}; rustc={}",settings,String::from_utf8_lossy(&compiler.stdout))));
    fs::write(PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("backend_build_inputs.rs"),generated).unwrap();
}
