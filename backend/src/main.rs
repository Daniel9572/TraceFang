mod store;
mod catalog;
mod capture;
mod quotes;
mod market;
mod stream;
mod providers;
mod ingestion;
mod pages;
mod api;
mod analysis;
mod replay;
mod history;
mod research;
mod quant_input;
mod batch_snapshot;
mod columnar_query;
mod columnar_api;

use anyhow::{Context,Result};
use std::{sync::Arc,time::Duration};
use tokio::sync::{watch,Mutex};

#[tokio::main]
async fn main()->Result<()> {
    if std::env::args().skip(1).any(|a|a=="--build-info") {
        println!("{}",api::build_info());return Ok(())
    }
    if std::env::args().skip(1).any(|a|a=="--version"||a=="-V") {
        println!("tracefang-server {}",env!("CARGO_PKG_VERSION"));return Ok(())
    }
    let _=dotenvy::from_filename(".env.local");let _=dotenvy::dotenv();
    tracing_subscriber::fmt().with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_|"tracefang_server=info,tower_http=warn".into())).init();
    let data=api::data_directory()?;
    let store_path=std::env::var("TRACEFANG_STORE_PATH").map(std::path::PathBuf::from).unwrap_or_else(|_|data.join("facts.redb"));
    let capture_path=std::env::var("TRACEFANG_CAPTURE_PATH").map(std::path::PathBuf::from).unwrap_or_else(|_|data.join("capture.redb"));
    let read_only=std::env::var("TRACEFANG_READ_ONLY_SHADOW").as_deref()==Ok("1");
    let generation=std::env::var("TRACEFANG_REHEARSAL_GENERATION").ok();
    anyhow::ensure!(generation.is_none() || read_only,"TRACEFANG_REHEARSAL_GENERATION requires true read-only shadow mode; a writer cannot select an inactive generation");
    if !read_only {std::fs::create_dir_all(&data)?;}
    let store=if let Some(generation)=generation {store::Store::connect_read_only_generation(store_path.to_str().context("storage path is not UTF-8")?,&generation).await?}
        else if read_only {store::Store::connect_read_only(store_path.to_str().context("storage path is not UTF-8")?).await?}
        else{store::Store::connect(store_path.to_str().context("storage path is not UTF-8")?).await?};store.migrate().await?;
    let capture=if read_only{capture::Capture::connect_read_only(capture_path.to_str().context("capture path is not UTF-8")?).await?}else{capture::Capture::connect(capture_path.to_str().context("capture path is not UTF-8")?).await?};
    let market=market::Market::new(catalog::Catalog::embedded()?,store.clone()).await?;
    if !read_only && store.metadata("sources","config").await?.is_none(){
        let config_path=std::env::var("TRACEFANG_SOURCE_CONFIG").map(std::path::PathBuf::from).unwrap_or_else(|_|std::path::PathBuf::from("data/sources.json"));
        if let Ok(bytes)=std::fs::read(config_path){store.set_metadata("sources","config",serde_json::from_slice(&bytes)?).await?;}
    }
    let (shutdown_tx,shutdown)=watch::channel(false);
    let (acquisition,mut tasks)=ingestion::Acquisition::start(market.clone(),capture.clone()).await?;
    let history=Arc::new(history::History::new(market.clone(),capture.clone(),acquisition.clone())?);
    let tail_task=if !read_only && std::env::var("TRACEFANG_ACQUISITION_ENABLED").as_deref()!=Ok("0"){Some(history.clone().spawn_tail_recovery(shutdown.clone()))}else{None};
    let http=providers::http_client()?;
    let root=std::env::current_dir()?;
    let python=std::env::var("TRACEFANG_PYTHON").map(std::path::PathBuf::from).unwrap_or_else(|_|root.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"}));
    let research=research::Research::new(http.clone(),data.join("research-cache"),python,root);
    let state=api::AppState{market:market.clone(),acquisition:acquisition.clone(),capture:capture.clone(),shutdown:shutdown.clone(),
        http,options_cache:Arc::new(Mutex::new(Default::default())),ai:Arc::new(analysis::ai::AiService::new()),history:history.clone(),research};
    let static_path=std::env::var("TRACEFANG_WEB_DIST").unwrap_or_else(|_|"web/dist".into());
    let app=api::router(state).fallback_service(tower_http::services::ServeDir::new(&static_path)
        .not_found_service(tower_http::services::ServeFile::new(format!("{static_path}/index.html"))))
        .layer(tower_http::trace::TraceLayer::new_for_http());
    let host=std::env::var("TRACEFANG_HOST").unwrap_or_else(|_|"127.0.0.1".into());
    let port=std::env::var("TRACEFANG_PORT").unwrap_or_else(|_|"8000".into()).parse::<u16>()?;
    let listener=tokio::net::TcpListener::bind((host.as_str(),port)).await?;
    tracing::info!(%host,port,pid=std::process::id(),"TraceFang Rust server listening");
    let shutdown_handle=tokio::spawn(async move {shutdown_requested().await;shutdown_tx.send_replace(true);});
    let mut stop=shutdown.clone();
    let result=axum::serve(listener,app).with_graceful_shutdown(async move{let _=stop.changed().await;}).await;
    let mut clean=true;
    if let Some(mut task)=tail_task {
        match tokio::time::timeout(Duration::from_secs(30),&mut task).await {
            Ok(Ok(()))=>{},
            Ok(Err(error))=>{clean=false;tracing::error!(%error,"history demand task failed during shutdown");},
            Err(_)=>{
                clean=false;tracing::error!("history demand did not stop cleanly");
                task.abort();
                if let Err(error)=task.await {tracing::error!(%error,"history demand aborted after shutdown timeout");}
            },
        }
    }
    match tokio::time::timeout(Duration::from_secs(60),tasks.stop_and_drain()).await {
        Ok(Ok(()))=>{},Ok(Err(error))=>{clean=false;tracing::error!(%error,"acquisition drain failed; restart must validate durable raw prefix");},
        Err(_)=>{clean=false;tracing::error!("acquisition drain timed out; unpersisted work may remain and shutdown is unclean");tasks.abort_and_join().await;},
    }
    if let Err(error)=capture.close_and_drain().await {clean=false;tracing::error!(%error,"capture durability drain failed");}
    if let Err(error)=store.close().await {clean=false;tracing::error!(%error,"canonical store close failed");}
    shutdown_handle.abort();
    result?;
    anyhow::ensure!(clean,"shutdown did not complete cleanly; captured data will replay on restart");
    Ok(())
}

async fn shutdown_requested(){
    let file=async {
        match std::env::var("TRACEFANG_SHUTDOWN_FILE") {
            Ok(path)=>loop{if tokio::fs::try_exists(&path).await.unwrap_or(false){break}tokio::time::sleep(Duration::from_millis(200)).await;},
            Err(_)=>std::future::pending::<()>().await,
        }
    };
    #[cfg(unix)] {
        let mut terminate=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("install SIGTERM handler");
        tokio::select!{_=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{},_=file=>{}}
    }
    #[cfg(not(unix))] tokio::select!{_=tokio::signal::ctrl_c()=>{},_=file=>{}}
}
