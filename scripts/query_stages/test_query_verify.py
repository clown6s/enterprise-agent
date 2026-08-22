# -*- coding: utf-8 -*-
"""真实校验/守卫阶段诊断；不写历史、反馈或评测状态。"""
from query_test_common import get_args, write_result
from app.service import retrieval, agent, verify

def main():
    args=get_args(); q=args.query
    try:
        hits=retrieval.retrieve(q,extra_queries=agent._expand_queries(q))
        ctx=retrieval.build_context(hits)
        raw=agent._llm_call(f"【资料】\n{ctx}\n\n【用户问题】\n{q}") if ctx.strip() else ''
        final=verify.verify(raw,ctx,query=q) if raw and ctx else raw
        result={'stage':'verify','query':q,'context_chars':len(ctx),'raw_answer':raw,
                'final_answer':final,'status':'ok'}
    except Exception as exc:
        result={'stage':'verify','query':q,'status':'error','error_type':type(exc).__name__,'error':str(exc)[:500]}
    write_result(q,'verify',result,args.output)
if __name__=='__main__': main()
