
import copy
import sys
import os
import json
import pickle
from .core import *
from datetime import date, datetime
import traceback


class ConfigManager():

    def __init__(self,provider,conf,instances,environments,jobs):
        
        self._provider = provider
        self._config = conf
        self._instances = instances
        self._environments = environments
        self._jobs = jobs

    def load(self):
        self._load_objects()
        self._preprocess_jobs()
        self._sanity_checks()

    def _load_objects(self):
        projectName = self._config.get('project')
        inst_cfgs   = self._config.get('instances')
        env_cfgs    = self._config.get('environments')
        job_cfgs    = self._config.get('jobs')

        #self._instances = [ ]
        if inst_cfgs:
            for inst_cfg in inst_cfgs:
                # virtually demultiply according to 'number' and 'explode'
                for i in range(inst_cfg.get('number',1)):
                    rec_cpus = self._provider.get_recommended_cpus(inst_cfg)
                    if rec_cpus is None:
                        self._provider.debug(1,"WARNING: could not set recommended CPU size for instance type:",inst_cfg.get('type'))
                        cpu_split = None
                    else:
                        cpu_split = rec_cpus[len(rec_cpus)-1]

                    if cpu_split is None:
                        if inst_cfg.get('cpus') is not None:
                            oldcpu = inst_cfg.get('cpus')
                            self._provider.debug(1,"WARNING: removing CPU reqs for instance type:",inst_cfg.get('type'),"| from",oldcpu,">>> None")
                        real_inst_cfg = copy.deepcopy(inst_cfg)
                        real_inst_cfg.pop('number',None)  # provide default value to avoid KeyError
                        real_inst_cfg.pop('explode',None) # provide default value to avoid KeyError

                        real_inst_cfg['cpus'] = None
                        real_inst_cfg['rank'] = "{0}.{1}".format(i+1,1)
                        # [!important!] also copy global information that are used for the name generation ...
                        real_inst_cfg['dev']  = self._config.get('dev',False)
                        real_inst_cfg['project']  = self._config.get('project',None)

                        # starts calling the Service here !
                        # instance , created = self.start_instance( real_inst_cfg )
                        # let's put some dummy instances for now ...
                        instance = CloudRunInstance( real_inst_cfg , None, None )
                        self._instances.append( instance )
                    else:
                        total_inst_cpus = inst_cfg.get('cpus',1)
                        if type(total_inst_cpus) != int and type(total_inst_cpus) != float:
                            cpucore = self.get_cpus_cores(inst_cfg)
                            total_inst_cpus = cpucore if cpucore is not None else 1
                            self._provider.debug(1,"WARNING: setting default CPUs number to",cpucore,"for instance",inst_cfg.get('type'))
                        if not inst_cfg.get('explode') and total_inst_cpus > cpu_split:
                            self._provider.debug(1,"WARNING: forcing 'explode' to True because required number of CPUs",total_inst_cpus,' is superior to ',inst_cfg.get('type'),'max number of CPUs',cpu_split)
                            inst_cfg.set('explode',True)

                        if inst_cfg.get('explode'):
                            num_sub_instances = math.floor( total_inst_cpus / cpu_split ) # we target CPUs with 16 cores...
                            if num_sub_instances == 0:
                                cpu_inc = total_inst_cpus
                                num_sub_instances = 1
                            else:
                                cpu_inc = cpu_split 
                                num_sub_instances = num_sub_instances + 1
                        else:
                            num_sub_instances = 1
                            cpu_inc = total_inst_cpus
                        cpus_created = 0 
                        for j in range(num_sub_instances):
                            rank = "{0}.{1}".format(i+1,j+1)
                            if j == num_sub_instances-1: # for the last one we're completing the cpus with whatever
                                inst_cpus = total_inst_cpus - cpus_created
                            else:
                                inst_cpus = cpu_inc
                            if inst_cpus == 0:
                                continue 

                            if rec_cpus is not None and not inst_cpus in rec_cpus:
                                self._provider.debug(1,"ERROR: The total number of CPUs required causes a sub-number of CPUs ( =",inst_cpus,") to not be accepted by the type",inst_cfg.get('type'),"| list of valid cpus:",rec_cpus)
                                sys.exit()

                            real_inst_cfg = copy.deepcopy(inst_cfg)
                            real_inst_cfg.pop('number',None)  # provide default value to avoid KeyError
                            real_inst_cfg.pop('explode',None) # provide default value to avoid KeyError
                            real_inst_cfg['cpus'] = inst_cpus
                            real_inst_cfg['rank'] = rank
                            # [!important!] also copy global information that are used for the name generation ...
                            real_inst_cfg['dev']  = self._config.get('dev',False)
                            real_inst_cfg['project']  = self._config.get('project',None)

                            # let's put some dummy instances for now ...
                            instance = CloudRunInstance( real_inst_cfg , None, None )
                            self._instances.append( instance )

                            cpus_created = cpus_created + inst_cpus
        
        self._provider.debug(3,self._instances)

        #self._environments = [ ] 
        if env_cfgs:
            for env_cfg in env_cfgs:
                # copy the dev global paramter to the environment configuration (will be used for names)
                env_cfg['dev']  = self._config.get('dev',False)
                env = CloudRunEnvironment(projectName,env_cfg)
                self._environments.append(env)

        #self._jobs = [ ] 
        if job_cfgs:
            for i,job_cfg in enumerate(job_cfgs):
                job = CloudRunJob(job_cfg,i)
                self._jobs.append(job)

    # fill up the jobs names if not present (and we have only 1 environment defined)
    # link the jobs objects with an environment object
    def _preprocess_jobs(self):
        for job in self._jobs:
            if not job.get_config('env_name'):
                if len(self._environments)==1:
                    job.attach_env(self._environments[0])
                else:
                    print("FATAL ERROR - you have more than one environments defined and the job doesnt have an env_name defined",job)
                    sys.exit()
            else:
                env = self._get_environment(job.get_config('env_name'))
                if not env:
                    print("FATAL ERROR - could not find env with name",job.get_config('env_name'),job)
                    sys.exit()
                else:
                    job.attach_env(env)

    def _sanity_checks(self):
        pass

    def _get_environment(self,name):
        for env in self._environments:
            if env.get_name() == name:
                return env
        return None

# def json_get_key(obj):
#     if isinstance(obj,CloudRunInstance):
#         return "__cloudrun_instance:" + obj.get_name()
#     elif isinstance(obj,CloudRunDeployedEnvironment):
#         return "__cloudrun_environment_dpl:" + obj.get_name()
#     elif isinstance(obj,CloudRunEnvironment):
#         return "__cloudrun_environment:" + obj.get_name()
#     elif isinstance(obj,CloudRunDeployedJob):
#         return "__cloudrun_job_dpl:" + obj.get_hash() + "|" + str(obj.get_job().get_rank()) + "," + str(obj.get_rank())
#     elif isinstance(obj,CloudRunJob):
#         return "__cloudrun_job:" + obj.get_hash() + "|" + str(obj.get_rank()) 
#     elif isinstance(obj,CloudRunProcess):
#         return "__cloudrun_process:" + obj.get_uid()
#     else:
#         return str(cr_obj)

# class CloudRunJSONEncoder(json.JSONEncoder):

#     def __init__(self, *args, **kwargs):
#         kwargs['check_circular'] = False  # no need to check anymore
#         super(CloudRunJSONEncoder,self).__init__(*args, **kwargs)
#         self.proc_objs = []

#     def default(self, obj):

#         if  isinstance(obj, (CloudRunInstance, CloudRunEnvironment, CloudRunDeployedEnvironment, CloudRunJob, CloudRunDeployedJob, CloudRunProcess)):
#             if obj in self.proc_objs:
#                 return json_get_key(obj)
#             else:
#                 self.proc_objs.append(obj)
#             return { **obj.__dict__ , **{'__class__':type(obj).__name__} }
        
#         elif isinstance(obj, (datetime, date)):
#             return obj.isoformat()  # Let the base class default method raise the TypeError

#         return super(CloudRunJSONEncoder,self).default(obj) #json.JSONEncoder.default(self, obj)

# class CloudRunJSONDecoder(json.JSONDecoder):
#     def __init__(self, *args, **kwargs):
#         json.JSONDecoder.__init__(self, object_hook=self.object_hook, *args, **kwargs)
#         self._references = dict()
    
#     def object_hook(self, dct):
#         #print(dct)
#         if '__class__' in dct:
#             class_name = dct['__class__']
#             try :
#                 if class_name == 'CloudRunInstance':
#                     obj = CloudRunInstance(dct['_config'],dct['_id'],dct['_data'])
#                     obj.__dict__.update(dct)
#                 elif class_name == 'CloudRunEnvironment':
#                     obj = CloudRunEnvironment(dct['_project'],dct['_config'])
#                     obj.__dict__.update(dct)
#                 elif class_name == 'CloudRunDeployedEnvironment':
#                     env_ref = None #self._references[dct['_env']]
#                     ins_ref = None #self._references[dct['_instance']]
#                     obj = CloudRunDeployedEnvironment.__new__(CloudRunDeployedEnvironment) #CloudRunDeployedEnvironment(env_ref,ins_ref)
#                     obj.__dict__.update(dct)
#                 elif class_name == 'CloudRunJob':
#                     obj = CloudRunJob(dct['_config'],dct['_rank'])
#                     obj.__dict__.update(dct)
#                 elif class_name == 'CloudRunDeployedJob':
#                     job_ref = None #self._references[dct['_job']]
#                     env_ref = None #self._references[dct['_env']]
#                     obj = CloudRunDeployedJob.__new__(CloudRunDeployedJob) #CloudRunDeployedJob(job_ref,env_ref)
#                     obj.__dict__.update(dct)
#                 elif class_name == 'CloudRunProcess':
#                     job_ref = None #self._references[dct['_job']]
#                     obj = CloudRunProcess.__new__(CloudRunProcess) #CloudRunProcess(job_ref,dct['_uid'],dct['_pid'],dct['_batch_uid'])
#                     obj.__dict__.update(dct)
#                 self._references[json_get_key(obj)] = obj
#                 return obj
#             except KeyError as ke:
#                 traceback.print_exc()
#                 print("KEY ERROR while DESERIALIZATION",ke)
#                 print(self._references)
#                 return None
#         else:
#             return dct        

class StateSerializer():

    def __init__(self,provider,instances,environments,jobs):
        self._provider = provider
        self._instances = instances
        self._environments = environments
        self._jobs = jobs

        self._state_file = 'state.pickle' #'state.json'
        self._loaded = None

    def serialize(self):
        try:
            state = {
                'instances' : self._instances ,
                'environments' :self._environments  ,
                'jobs' : self._jobs
            }
            #json_data = json.dumps(state,indent=4,cls=CloudRunJSONEncoder)
            with open(self._state_file,'wb') as state_file:
                pickle.dump(state,state_file)#,protocol=0) # protocol 0 = readable
                #state_file.write(json_data)
        except Exception as e:
            self._provider.debug(1,"SERIALIZATION ERROR",e)


    def load(self):
        if not os.path.isfile(self._state_file):
            self._provider.debug(2,"StateSerializer: no state serialized")
            return False
        try:
            with open(self._state_file,'rb') as state_file:
                #json_data = state_file.read()
                #objects   = json.loads(json_data,cls=CloudRunJSONDecoder)
                self._loaded = pickle.load(state_file)
        except Exception as e:
            traceback.print_exc()
            self._provider.debug(1,"DE-SERIALIZATION ERROR",e)

    # check if the state is consistent with the provider objects that have been loaded from the config...
    # TODO: do that
    def check_consistency(self):
        if self._loaded is None:
            self._provider.debug(2,"Seralized data not loaded. No consistency")
            return False 
        try:
            instances    = self._loaded['instances']
            environments = self._loaded['environments']
            jobs         = self._loaded['jobs']
            assert len(self._instances)==len(instances)
            assert len(self._environments)==len(environments)
            assert len(self._jobs)==len(jobs)
            
            for i,instance in enumerate(self._instances):
                assert instance.get_name() == instances[i].get_name()
                assert instance.get_cpus() == instances[i].get_cpus()
            for i,env in enumerate(self._environments):
                assert env.get_name() == environments[i].get_name()
            for i,job in enumerate(self._jobs):
                assert job.get_hash() == jobs[i].get_hash()
                assert job.get_rank() == jobs[i].get_rank()
            return True
        except Exception as e:
            self._provider.debug(1,e)
            return False

    def transfer(self):
        return self._loaded['instances'] , self._loaded['environments'] , self._loaded['jobs']