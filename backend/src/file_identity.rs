//! Exact local file identity for immutable snapshots and pinned executable evidence.
use anyhow::{Result,Context,ensure};
use std::{fs::{File,OpenOptions},path::{Path,PathBuf},io::{Read,Seek,SeekFrom}};
use sha2::{Digest,Sha256};

#[derive(Clone,Debug,PartialEq,Eq)]pub struct Identity {path:PathBuf,device:u64,inode:u64,bytes:u64,modified:(i64,i64),changed:(i64,i64)}
pub struct BoundFile {pub file:File,pub identity:Identity}
fn open(path:&Path)->Result<File>{let mut options=OpenOptions::new();options.read(true);
 #[cfg(windows)]{use std::os::windows::fs::OpenOptionsExt;options.share_mode(1);}
 Ok(options.open(path)?)}
fn identity(path:PathBuf,file:&File)->Result<Identity>{
 let metadata=file.metadata()?;
 #[cfg(unix)]{use std::os::unix::fs::MetadataExt;return Ok(Identity{path,device:metadata.dev(),inode:metadata.ino(),bytes:metadata.len(),modified:(metadata.mtime(),metadata.mtime_nsec()),changed:(metadata.ctime(),metadata.ctime_nsec())});}
 #[cfg(windows)]{use std::os::windows::io::AsRawHandle;
  #[repr(C)]struct FileTime{low:u32,high:u32}
  #[repr(C)]struct Info{attributes:u32,created:FileTime,accessed:FileTime,written:FileTime,volume:u32,size_high:u32,size_low:u32,links:u32,index_high:u32,index_low:u32}
  #[repr(C)]struct Basic{created:i64,accessed:i64,written:i64,changed:i64,attributes:u32}
  #[link(name="kernel32")]unsafe extern "system"{fn GetFileInformationByHandle(handle:*mut std::ffi::c_void,info:*mut Info)->i32;fn GetFileInformationByHandleEx(handle:*mut std::ffi::c_void,class:i32,info:*mut std::ffi::c_void,size:u32)->i32;}
  let mut info=std::mem::MaybeUninit::<Info>::uninit();let mut basic=std::mem::MaybeUninit::<Basic>::uninit();unsafe{ensure!(GetFileInformationByHandle(file.as_raw_handle(),info.as_mut_ptr())!=0&&GetFileInformationByHandleEx(file.as_raw_handle(),0,basic.as_mut_ptr().cast(),std::mem::size_of::<Basic>() as u32)!=0,"exact Windows file identity unavailable: {}",std::io::Error::last_os_error());let info=info.assume_init();let basic=basic.assume_init();return Ok(Identity{path,device:info.volume as u64,inode:((info.index_high as u64)<<32)|info.index_low as u64,bytes:metadata.len(),modified:(basic.written,0),changed:(basic.changed,0)});}
 }
 #[cfg(not(any(unix,windows)))]anyhow::bail!("exact local file identity unavailable on this platform")
}
impl BoundFile {
 pub fn open(path:&Path)->Result<Self>{let path=path.canonicalize()?;let file=open(&path)?;let identity=identity(path,&file)?;let result=Self{file,identity};result.guard()?;Ok(result)}
 pub fn path(&self)->&Path{&self.identity.path}
 pub fn guard(&self)->Result<()>{ensure!(self.path().canonicalize()?==self.identity.path,"immutable canonical path changed");ensure!(identity(self.identity.path.clone(),&self.file)?==self.identity,"held immutable file metadata changed");let fresh=open(self.path())?;ensure!(identity(self.identity.path.clone(),&fresh)?==self.identity,"immutable path now refers to a different file");Ok(())}
 pub fn reader(&self)->Result<File>{self.guard()?;let file=open(self.path())?;ensure!(identity(self.identity.path.clone(),&file)?==self.identity,"immutable file changed while opening reader");Ok(file)}
 pub fn sha256(&self)->Result<String>{self.guard()?;let mut file=self.reader()?;file.seek(SeekFrom::Start(0))?;let mut hash=Sha256::new();let mut bytes=[0u8;65536];loop{let n=file.read(&mut bytes)?;if n==0{break}hash.update(&bytes[..n]);}self.guard()?;Ok(hex::encode(hash.finalize()))}
 pub fn child_path(file:&File,fallback:&Path)->Result<String>{
  #[cfg(unix)]{use std::os::unix::io::AsRawFd;let _=fallback;return Ok(format!("/dev/fd/{}",file.as_raw_fd()));}
  #[cfg(not(unix))]{let _=file;Ok(fallback.to_str().context("child file path is not UTF8")?.into())}
 }
}
#[cfg(unix)]pub fn inherit(file:&File,command:&mut tokio::process::Command){use std::os::unix::io::AsRawFd;let fd=file.as_raw_fd();unsafe{command.pre_exec(move||{if libc::fcntl(fd,libc::F_SETFD,0)<0{return Err(std::io::Error::last_os_error())}Ok(())});}}
#[cfg(not(unix))]pub fn inherit(_file:&File,_command:&mut tokio::process::Command){}
