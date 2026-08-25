import {describe,expect,it} from "vitest";
import {buildPaperQuery} from "./model";

describe("paper query",()=>{
  it("encodes filters without losing unicode",()=>{
    const value=buildPaperQuery({page:2,query:"KV 缓存",venue:"OSDI",year:"2025",org:"NVIDIA",sort:"citations_desc"});
    const query=new URLSearchParams(value);
    expect(query.get("page")).toBe("2");expect(query.get("q")).toBe("KV 缓存");expect(query.get("sort")).toBe("citations_desc");
  });
});
