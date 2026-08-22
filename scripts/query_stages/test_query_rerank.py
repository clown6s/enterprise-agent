# -*- coding: utf-8 -*-
"""真实 rerank 阶段诊断。"""
from query_test_common import get_args, write_result, hit_view
from app.service import retrieval, agent
from app import embeddings, config


def main():
    args=get_args(); q=args.query; extra=agent._expand_queries(q)
    try:
        hits=retrieval.retrieve(q, extra_queries=extra)
        scores=embeddings.rerank(q, [h.get('text','') for h in hits], top_n=len(hits)) if hits else []
        result={'stage':'rerank','query':q,'candidate_count':len(hits),
                'scores':[{'index':i,'score':s,'hit':hit_view(hits[i])} for i,s in scores],
                'min_score':config.RERANK_MIN_SCORE,'status':'ok'}
    except Exception as exc:
        result={'stage':'rerank','query':q,'candidate_count':0,'scores':[],
                'status':'error','error_type':type(exc).__name__,'error':str(exc)[:500]}
    write_result(q,'rerank',result,args.output)

if __name__=='__main__': main()
