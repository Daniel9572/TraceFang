import {useEffect, useRef, useState} from "react";
import {quantApi, type ResearchSnapshotReference, type QuantBuildProgress} from "./quantApi";
import type {QuantSnapshot} from "./quantTypes";

/** Coordinates only identify a view. Price inputs remain server owned. */
export function useQuantSnapshot(code:string, sourceId:string, period:string, revision:string, enabled=true, research?:ResearchSnapshotReference, strategies?:readonly string[]) {
  const [snapshot,setSnapshot]=useState<QuantSnapshot|null>(null);
  const snapshotRef=useRef<QuantSnapshot|null>(null);snapshotRef.current=snapshot;
  const [error,setError]=useState<string|null>(null);
  const [progress,setProgress]=useState<QuantBuildProgress|null>(null);
  const strategyKey=strategies?.join(",")??"default";
  const identity=`${code}:${sourceId}:${period}:${research?.research_snapshot_id??""}:${research?.research_asset??""}:${research?.research_adjustment??""}:${strategyKey}`;
  const identityRef=useRef(identity);identityRef.current=identity;
  useEffect(()=>{snapshotRef.current=null;setSnapshot(null);setError(null);setProgress(null);},[identity,enabled]);
  useEffect(()=>{
    if(!enabled)return;
    const abort=new AbortController();
    const timer=setTimeout(()=>{
      void quantApi.defaults().then(defaults=>quantApi.snapshot({code,source_id:sourceId,period,parameters:strategies?{...defaults.parameters,enabled_strategies:[...strategies].sort()}:defaults.parameters,...research},abort.signal,value=>{if(!abort.signal.aborted&&identityRef.current===identity)setProgress(value);},snapshotRef.current)).then(value=>{
        if(!abort.signal.aborted&&identityRef.current===identity){setSnapshot(value);setError(null);}
      }).catch(failure=>{if(!abort.signal.aborted&&identityRef.current===identity)setError(failure instanceof Error?failure.message:String(failure));});
    },250);
    return()=>{clearTimeout(timer);abort.abort();};
  },[code,sourceId,period,revision,identity,enabled,research?.research_snapshot_id,research?.research_asset,research?.research_adjustment,strategyKey]);
  return {snapshot,error,progress};
}
