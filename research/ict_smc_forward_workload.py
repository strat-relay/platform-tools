"""Kubernetes documents generated only from a concrete reviewed run and digest."""
import json
from .ict_smc_forward_plan import WORKLOAD


def documents(manifest,image, *, workload=WORKLOAD):
    labels={'app.kubernetes.io/name':workload,'app.kubernetes.io/component':'research-observer','stratrelay.app/status':'research'}
    config_name=manifest['run_id']+'-manifest'
    probe=['python','-m','research.strategy_library_forward','health','--manifest','/config/run.json']
    cycle_check=['python','-c',"import time; from postgres.db import connect; from research.strategy_library_forward import RunConfig; c=RunConfig.load('/config/run.json'); conn=connect(); cur=conn.cursor(); cur.execute(\"SELECT (state->'health'->>'observed_at')::double precision FROM research.strategy_library_forward_state WHERE run_id=%s\",(c.run_id,)); r=cur.fetchone(); conn.rollback(); conn.close(); raise SystemExit(0 if r and 0<=time.time()-r[0]<=900 else 1)"]
    container={'name':'observer','image':image,'command':['python','-m','research.strategy_library_forward','run','--manifest','/config/run.json','--interval','10'],
      'envFrom':[{'secretRef':{'name':'strategy-library-observer-db'}}],
      'env':[{'name':'REDIS_PASSWORD','valueFrom':{'secretKeyRef':{'name':'trading-redis-auth','key':'password'}}},
             {'name':'MARKET_DATA_REDIS_URL','value':'redis://:$(REDIS_PASSWORD)@trading-redis.trading.svc:6379/0'},
             {'name':'PGCONNECT_TIMEOUT','value':'10'},{'name':'EXECUTION_AUTHORITY_MODE','value':'DISABLED'},{'name':'SIGNAL_AUTHORITY_MODE','value':'RESEARCH_ONLY'}],
      'resources':{'requests':{'cpu':'200m','memory':'256Mi'},'limits':{'cpu':'1000m','memory':'768Mi'}},
      'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
      'volumeMounts':[{'name':'manifest','mountPath':'/config','readOnly':True},{'name':'tmp','mountPath':'/tmp'}],
      'readinessProbe':{'exec':{'command':probe},'periodSeconds':30,'timeoutSeconds':15,'failureThreshold':3},
      'startupProbe':{'exec':{'command':cycle_check},'periodSeconds':30,'timeoutSeconds':15,'failureThreshold':40},
      'livenessProbe':{'exec':{'command':cycle_check},'periodSeconds':60,'timeoutSeconds':15,'failureThreshold':3}}
    deployment={'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':workload,'namespace':'trading','labels':labels},
      'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app.kubernetes.io/name':workload}},
        'template':{'metadata':{'labels':labels},'spec':{'automountServiceAccountToken':False,'imagePullSecrets':[{'name':'ghcr-pull'}],
          'securityContext':{'runAsNonRoot':True,'runAsUser':65532,'runAsGroup':65532,'seccompProfile':{'type':'RuntimeDefault'}},
          'containers':[container],'volumes':[{'name':'manifest','configMap':{'name':config_name}},{'name':'tmp','emptyDir':{}}]}}}}
    egress=[{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'trading'}},'podSelector':{'matchLabels':{'app.kubernetes.io/name':name}}}],
             'ports':[{'port':port,'protocol':'TCP'}]} for name,port in [('trading-postgres',5432),('trading-redis',6379)]]
    egress.append({'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}}}],
                   'ports':[{'port':53,'protocol':'UDP'},{'port':53,'protocol':'TCP'}]})
    return {'apiVersion':'v1','kind':'List','items':[
      {'apiVersion':'v1','kind':'ConfigMap','immutable':True,'metadata':{'name':config_name,'namespace':'trading'},'data':{'run.json':json.dumps(manifest,indent=2)}},
      deployment,{'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':workload+'-egress','namespace':'trading'},
        'spec':{'podSelector':{'matchLabels':{'app.kubernetes.io/name':workload}},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':egress}}]}
