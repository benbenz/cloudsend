import asyncio
import shutil
import os
import traceback
from datetime import datetime
from scriptflow.runners import AbstractRunner
from cloudsend.provider import get_client
from cloudsend.core import CloudSendProcessState
from cloudsend.utils import generate_unique_id

TASK_PROP_UID = 'cloudsend_uid'
TASK_DICT_HANDLED = '_handled'
JOB_CFG_T_UID = 'task_uid'
SLEEP_PERIOD_SHORT = 0.1
SLEEP_PERIOD_LONG  = 15

# Runner for tlamadon/scriptflow

class CloudSendRunner(AbstractRunner):

    def __init__(self, conf, handle_task_queue=True):
        self._cloudsend         = get_client(conf)
        self._run_session       = None
        self._processes         = {}
        self._num_instances     = 0 
        self._handle_task_queue = handle_task_queue
        self._sleep_period      = SLEEP_PERIOD_SHORT

    def _move_results(self,results_dir):
        for root, dirs, files in os.walk(results_dir):
            for filename in files:
                moved_file = os.path.join(os.getcwd(),filename)
                if os.path.isfile(moved_file):
                    os.remove(moved_file)
                shutil.move(os.path.join(results_dir,filename),os.getcwd())

            for dirname in dirs:
                moved_dir = os.path.join(os.getcwd(),dirname)
                if os.path.isdir(moved_dir):
                    shutil.rmtree(moved_dir)
                shutil.move(os.path.join(results_dir,dirname),os.getcwd())

    def _associate_jobs_to_tasks(self,objects):
        # associate the job objects with the tasks
        for (k,p) in self._processes.items():
            if p["job"]:
                continue
            if p.get(TASK_DICT_HANDLED,False)==False:
                continue
            found = False
            for job in objects['jobs']:
                if job.get_config(JOB_CFG_T_UID) == p["task"].get_prop(TASK_PROP_UID):
                    p["job"] = job
                    found = True
                    break
            if not found:
                # this can happen due to asynchronisms
                #raise Error("Internal Error: could not find job for task")        
                pass

    def size(self):
        return(len(self._processes))

    def available_slots(self):
        if self._handle_task_queue:
            # always available slots
            # this will cause the controller to add all the tasks at once
            # cloudsend will handle the stacking with its own batch_run feature ...
            return self._num_instances
        else:
            return self._num_instances - len(self._processes)

    """
        Start tasks
    """

    def add(self, task):

        # we're not doing much here because this method is not async ...
        # leave it to the update(..) method to do the work...
        # we also want to group the handling of adding jobs and running them

        # we don't want to wait as much when we are adding tasks >> lower the period
        self._sleep_period = SLEEP_PERIOD_SHORT 

        # let's not touch the name/uid of the task
        # and it may be empty ...
        task.set_prop(TASK_PROP_UID,generate_unique_id())

        self._processes[task.hash] = {
            "task": task , 
            "job":  None , # we have a one-to-one relationship between job and task (no demultiplier)
            "state" : CloudSendProcessState.UNKNOWN ,
            'start_time': datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }

    """
        Continuously checks on the tasks
    """
    async def loop(self, controller):

        # start the provider and get remote instances information
        await self._cloudsend.start()
        # delete the fetch results directory
        await self._cloudsend.clear_results_dir()

        # cache the number of instances
        self._num_instances = await self._cloudsend.get_num_instances()

        while True:
            try:
                await self._update(controller)                
                await asyncio.sleep(self._sleep_period)
            except Exception as e:
                print("Loop Error",e)
                traceback.print_exc()
                break

    async def _update(self,controller):

        jobs_cfg = []

        job_added    = True
        while job_added:
            job_added = False
            for (k,p) in self._processes.items():
                # create the job if its not there (it will append/run automatically)
                if p["job"]:
                    continue 

                if p.get(TASK_DICT_HANDLED,False) == True:
                    continue

                task = p["task"]

                job_cfg = {
                    'input_files'  : task.deps ,
                    'output_files' : task.outputs[None],
                    #'run_command'  : shlex.join(task.get_command()) ,
                    'run_command'  : " ".join(task.get_command()) ,
                    'cpus_req'     : task.ncore ,
                    'number'       : 1 ,
                    JOB_CFG_T_UID  : task.get_prop(TASK_PROP_UID)
                }

                jobs_cfg.append( job_cfg )

                job_added     = True
                p[TASK_DICT_HANDLED] = True
            
            # we've just added a task let's see if another one comes in ...
            await asyncio.sleep(.5)

        # add the new jobs and get jobs objects back
        if len(jobs_cfg)>0:

            print("ADDING JOBS",jobs_cfg)

            objects = await self._cloudsend.cfg_add_jobs(jobs_cfg,run_session=self._run_session)

            # 1 task <-> 1 job
            assert( objects and len(objects['jobs']) == len(jobs_cfg) )

            # run the jobs
            if self._run_session is None:
                await self._cloudsend.deploy() # deploy the new jobs etc.
                self._run_session = await self._cloudsend.run() 
            else:
                await self._cloudsend.deploy() # deploy the new jobs etc.
                await self._cloudsend.run(True) # continue_session

            # associate task <-> job
            self._associate_jobs_to_tasks(objects)

        # fetch statusses here ...
        # True stands for 'last_running_processes' meaning we will only get one process per job (the last one)
        processes_states = await self._cloudsend.get_jobs_states(self._run_session,True)

        # augment the period now ...
        if len(processes_states)>0:
            self._sleep_period = SLEEP_PERIOD_LONG

        #update status
        to_remove = []
        for (k,p) in self._processes.items():
            job = p["job"]
            if not job: # this task hadnt been handled yet
                continue 
            if p.get(TASK_DICT_HANDLED,False)==False:
                continue
            found_process = False
            for pstatus in processes_states.values():
                if pstatus['job_config'][JOB_CFG_T_UID] == p["task"].get_prop(TASK_PROP_UID):

                    assert pstatus['job_rank'] == job.get_rank() and pstatus['job_hash'] == job.get_hash() , "Internal Error: task<->job<->last_process: it looks like something is wrong ! Fix it"
                    
                    p["state"] = pstatus['state']
                    found_process = True
                    break
            
            # should not happen
            assert found_process , "Internal Error: task<->job<->last_process: We couldn't find a process in the results"

            if p["state"] & ( CloudSendProcessState.DONE | CloudSendProcessState.ABORTED):
                to_remove.append(k)

        # we got some jobs to fetch
        if len(to_remove)>0:
            results_dir = await self._cloudsend.fetch_results('tmp',self._run_session,True,True)
            self._move_results(results_dir)

        # signal completed tasks
        for k in to_remove:
            task = self._processes[k]["task"]
            del self._processes[k]       
            controller.add_completed( task )