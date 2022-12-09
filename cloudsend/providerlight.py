
from abc import ABC , abstractmethod
from cloudsend.provider import CloudSendProvider , line_buffered 
from cloudsend.core import *
import copy , io
from zipfile import ZipFile
import os , fnmatch
import re
from os.path import basename
import pkg_resources
import time
import random
import shutil

random.seed()

####################################
# Client handling MAESTRO instance #
####################################

class CloudSendLightProvider(CloudSendProvider,ABC):

    def __init__(self,conf):
        CloudSendProvider.__init__(self,conf)
        self._load()

        self.ssh_conn = None
        self.ftp_client = None

        #self._install_maestro()
    
    def _load(self):
        self._maestro = None
        if self._config.get('maestro')=='remote':
            if not self._config.get('instances') or len(self._config.get('instances'))==0:
                self.debug(2,"There are no instances to watch - skipping maestro creation")
                return
            else:
                # let's try to match the region of the first instance ...
                region = None
                if len(self._config.get('instances')) > 0:
                    region = self._config.get('instances')[0].get('region')
                if not region:
                    region = self.get_region()

                img_id , img_user , img_type = self.get_suggested_image(region)
                if not img_id:
                    self.debug(1,"Using first instance information to create MAESTRO")
                    img_id   = self._config.get('instances')[0].get('img_id')
                    img_user = self._config.get('instances')[0].get('img_username')
                    img_type = self._config.get('instances')[0].get('img_type')
                
                maestro_cfg = { 
                    'maestro'      : True ,
                    'img_id'       : img_id ,
                    'img_username' : img_user ,                 
                    'type'         : img_type , 
                    'dev'          : self._config.get('dev',False) ,
                    'project'      : self._config.get('project',None) ,
                    'region'       : region
                }
                self._maestro = CloudSendInstance(maestro_cfg,None)

    async def _deploy_maestro(self,reset):
        # deploy the maestro ...
        if self._maestro is None:
            self.debug(2,"no MAESTRO object to deploy")
            return

        # wait for maestro to be ready
        instanceid , ssh_conn , ftp_client = await self._wait_and_connect(self._maestro)

        home_dir = self._maestro.get_home_dir()
        cloudsend_dir = self._maestro.path_join( home_dir , 'cloudsend' )
        files_dir = self._maestro.path_join( cloudsend_dir , 'files' )
        ready_file = self._maestro.path_join( cloudsend_dir , 'ready' )
        maestro_file = self._maestro.path_join( home_dir , 'maestro' )
        aws_dir = self._maestro.path_join( home_dir , '.aws' )
        aws_config = self._maestro.path_join( aws_dir , 'config' )
        if self._maestro.get_platform() == CloudSendPlatform.LINUX or self._maestro.get_platform() == CloudSendPlatform.WINDOWS_WSL :
            activate_file = self._maestro.path_join( cloudsend_dir , '.venv' , 'maestro' , 'bin' , 'activate' )
        elif self._maestro.get_platform() == CloudSendPlatform.WINDOWS:
            activate_file = self._maestro.path_join( cloudsend_dir , '.venv' , 'maestro' , 'Scripts' , 'activate.bat' )
        
        re_init  = await self._test_reupload(self._maestro,ready_file, ssh_conn)

        if re_init:
            # remove the file
            await self._exec_command(ssh_conn,'rm -f ' + ready_file )
            # make cloudsend dir
            await self._exec_command(ssh_conn,'mkdir -p ' + files_dir ) 
            # mark it as maestro...
            await self._exec_command(ssh_conn,'echo "" > ' + maestro_file ) 
            # add manually the 
            if self._config.get('profile'):
                profile = self._config.get('profile')
                region  = self.get_region() # we have a profile so this returns the region for this profile
                aws_config_cmd = "mkdir -p "+aws_dir+" && echo \"[profile "+profile+"]\nregion = " + region + "\noutput = json\" > " + aws_config
                await self._exec_command(ssh_conn,aws_config_cmd)
            # grant its admin rights (we need to be (stopped or) running to be able to do that)
            self.grant_admin_rights(self._maestro)
            # setup auto_stop behavior for maestro
            self.setup_auto_stop(self._maestro)
            # deploy CloudSend on the maestro
            await self._deploy_cloudsend(ssh_conn,ftp_client)
            # mark as ready
            await self._exec_command(ssh_conn,'if [ -f '+activate_file+' ]; then echo "" > '+ready_file+' ; fi')

        # deploy the config to the maestro (every time)
        await self._deploy_config(ssh_conn,ftp_client)

        # let's redeploy the code every time for now ... (if not already done in re_init)
        if not re_init and reset:
            await self._deploy_cloudsend_files(ssh_conn,ftp_client)

        # start the server (if not already started)
        await self._run_server(ssh_conn)

        # wait for maestro to have started
        await self._wait_for_maestro(ssh_conn)

        #time.sleep(30)

        self.ssh_conn = ssh_conn
        self.ftp_client = ftp_client
        #ftp_client.close()
        #ssh_conn.close()

        self.debug(1,"MAESTRO is READY",color=bcolors.OKCYAN)


    async def _deploy_cloudsend_files(self,ssh_conn,ftp_client):
        await ftp_client.chdir(self._get_cloudsend_dir())
        # CP437 is IBM zip file encoding
        await self.sftp_put_bytes(ftp_client,'cloudsend.zip',self._create_cloudsend_zip(),'CP437')
        commands = [
            { 'cmd' : 'cd '+self._get_cloudsend_dir()+' && unzip -o cloudsend.zip && rm cloudsend.zip' , 'out' : True } ,
        ]
        await self._run_ssh_commands(self._maestro,ssh_conn,commands) 
        
        filesOfDirectory = os.listdir('.')
        pattern = "cloudsend*.pem"
        for file in filesOfDirectory:
            if fnmatch.fnmatch(file, pattern):
                await ftp_client.put(os.path.abspath(file),os.path.basename(file))
        sh_files = self._get_remote_files_path( '*.sh')
        commands = [
            { 'cmd' : 'chmod +x ' + sh_files , 'out' : True } ,
        ]
        await self._run_ssh_commands(self._maestro,ssh_conn,commands) 


    async def _deploy_cloudsend(self,ssh_conn,ftp_client):

        # upload cloudsend files
        await self._deploy_cloudsend_files(ssh_conn,ftp_client)
        
        maestroenv_sh = self._get_remote_files_path( 'maestroenv.sh' )
        # unzip cloudsend files, install and run
        commands = [
            { 'cmd' : maestroenv_sh , 'out' : True } ,
        ]
        await self._run_ssh_commands(self._maestro,ssh_conn,commands)

    async def _run_server(self,ssh_conn):
        # run the server
        startmaestro_sh = self._get_remote_files_path( 'startmaestro.sh' )
        commands = [
            { 'cmd' : startmaestro_sh , 'out' : False , 'output' : 'maestro.log' },
            { 'cmd' : 'crontab -r ; echo "* * * * * '+startmaestro_sh+'" | crontab', 'out' : True }
        ]
        await self._run_ssh_commands(self._maestro,ssh_conn,commands)

    async def _deploy_config(self,ssh_conn,ftp_client):
        config , mkdir_cmd , files_to_upload_per_dir = self._translate_config_for_maestro()
        # serialize the config and send it to the maestro
        await ftp_client.chdir(self._get_cloudsend_dir())
        await self.sftp_put_string(ftp_client,'config.json',json.dumps(config))
        # execute the mkdir_cmd
        if mkdir_cmd:
            await self._exec_command(ssh_conn,mkdir_cmd)
        for remote_dir , files_infos in files_to_upload_per_dir.items():
            await ftp_client.chdir(remote_dir)
            for file_info in files_infos:
                remote_file = os.path.basename(file_info['remote'])
                try:
                    await ftp_client.put(file_info['local'],remote_file)
                except FileNotFoundError as fnfe:
                    self.debug(1,"You have specified a file that does not exist:",fnfe,color=bcolors.FAIL)

    def _translate_config_for_maestro(self):
        config = copy.deepcopy(self._config)

        config['maestro'] = 'local' # the config is for maestro, which needs to run local

        config['region'] = self.get_region()

        for i , inst_cfg in enumerate(config.get('instances')):
            # we need to freeze the region within each instance config ...
            if not config['instances'][i].get('region'):
                config['instances'][i]['region'] = self.get_region()

        for i , env_cfg in enumerate(config.get('environments')):
            # Note: we are using a simple CloudSendEnvironment here 
            # we're not deploying at this stage
            # but this will help use re-use all the business logic in cloudsendutils envs
            # in order to standardize the environment and create an inline version 
            # through the CloudSendEnvironment::json() method
            env = CloudSendEnvironment(self._config.get('project'),env_cfg)
            config['environments'][i] = env.get_env_obj()
            config['environments'][i]['_computed_'] = True

        files_to_upload = dict()

        for i , job_cfg in enumerate(config.get('jobs')):
            job = CloudSendJob(job_cfg,i)
            run_script = job.get_config('run_script')  
            ref_file0  = run_script
            args       = ref_file0.split(' ')
            if args:
                ref_file0 = args[0]
            #for attr,use_ref in [ ('run_script',False) , ('upload_files',True) , ('input_file',True) , ('output_file',True) ] :
            for attr,use_ref in [ ('run_script',False) , ('upload_files',True) , ('input_file',True) ] :
                upfiles = job_cfg.get(attr)
                ref_file = ref_file0 if use_ref else None
                if not upfiles:
                    continue
                if isinstance(upfiles,str):
                    upfiles_split = upfiles.split(' ')
                    if len(upfiles_split)>0:
                        upfiles = upfiles_split[0]
                if isinstance(upfiles,str):
                    local_abs_path , local_rel_path , remote_abs_path , remote_rel_path , external = self._resolve_maestro_job_paths(upfiles,ref_file,self._get_home_dir())
                    if local_abs_path not in files_to_upload:
                        files_to_upload[local_abs_path] = { 'local' : local_abs_path , 'remote' : remote_abs_path }
                    if attr == 'run_script':
                        args = run_script.split(' ')
                        args[0] = remote_abs_path
                        config['jobs'][i][attr] = (' ').join(args)
                    else:
                        config['jobs'][i][attr] = remote_abs_path
                else:
                    config['jobs'][i][attr] = []
                    for upfile in upfiles:
                        local_abs_path , local_rel_path , remote_abs_path , remote_rel_path , external = self._resolve_maestro_job_paths(upfile,ref_file,self._get_home_dir())
                        if local_abs_path not in files_to_upload:
                            files_to_upload[local_abs_path] = { 'local' : local_abs_path , 'remote' : remote_abs_path }
                        config['jobs'][i][attr].append(remote_abs_path)
        
        mkdir_cmd = ""
        files_to_upload_per_dir = dict()
        for key,file_info in files_to_upload.items():
            remote_dir = os.path.dirname(file_info['remote'])
            if remote_dir not in files_to_upload_per_dir:
                files_to_upload_per_dir[remote_dir] = []
                mkdir_cmd += "mkdir -p " + remote_dir + ";"
            files_to_upload_per_dir[remote_dir].append(file_info)

        self.debug(2,config)

        return config , mkdir_cmd , files_to_upload_per_dir

    def _resolve_maestro_job_paths(self,upfile,ref_file,home_dir):
        files_path = self._maestro.path_join( home_dir , 'files' )
        return cloudsendutils.resolve_paths(self._maestro,upfile,ref_file,files_path,True) # True for mutualized 

    def _get_home_dir(self):
        return self._maestro.get_home_dir()

    def _get_cloudsend_dir(self):
        return self._maestro.path_join( self._get_home_dir() , 'cloudsend' )

    def _get_remote_files_path(self,files=None):
        if not files:
            return self._maestro.path_join( self._get_cloudsend_dir() , 'cloudsend' , 'resources' , 'remote_files' )        
        else:
            return self._maestro.path_join( self._get_cloudsend_dir() , 'cloudsend' , 'resources' , 'remote_files' , files )        

    def _zip_package(self,package_name,src,dest,zipObj):
        dest = os.path.join( dest , os.path.basename(src) ).rstrip( os.sep )
        if pkg_resources.resource_isdir(package_name, src):
            #if not os.path.isdir(dest):
            #    os.makedirs(dest)
            for res in pkg_resources.resource_listdir(package_name, src):
                self.debug(2,'scanning package resource',res)
                self._zip_package(package_name,os.path.join( src , res), dest, zipObj)
        else:
            if os.path.splitext(src)[1] not in [".pyc"] and not src.strip().endswith(".DS_Store"):
                #copy_resource_file(src, dest) 
                data_str = pkg_resources.resource_string(package_name, src)
                self.debug(2,"Writing",src)
                zipObj.writestr(dest,data_str)

    def _create_cloudsend_zip(self):
        zip_buffer = io.BytesIO()
        # create a ZipFile object
        with ZipFile(zip_buffer, 'w') as zipObj:    
            for otherfilepath in ['pyproject.toml' , 'requirements.txt' ]:
                with open(otherfilepath,'r') as thefile:
                    data = thefile.read()
                    zipObj.writestr(otherfilepath,data)
            # write the package
            self._zip_package("cloudsend",".","cloudsend",zipObj)

        self.debug(2,"ZIP BUFFER SIZE =",zip_buffer.getbuffer().nbytes)

        # very important...
        zip_buffer.seek(0)

        return zip_buffer.getvalue() #.getbuffer()

    async def _wait_for_maestro(self,ssh_conn):
        #if self.ssh_conn is None:
        #    instanceid , self.ssh_conn , self.ftp_client = self._wait_and_connect(self._maestro)
        
        waitmaestro_sh = self._get_remote_files_path( 'waitmaestro.sh' )
        cmd = waitmaestro_sh+" 1" # 1 = with tail log

        stdout , stderr = await self._exec_command(ssh_conn,cmd)      

        async for line in proc.stdout:
            self.debug(1,line,end='')

        # for l in await line_buffered(stdout):
        #     if not l:
        #         break
        #     self.debug(1,l,end='')


    async def _exec_maestro_command(self,maestro_command):
        if self.ssh_conn is None:
            instanceid , self.ssh_conn , self.ftp_client = await self._wait_and_connect(self._maestro)
        
        private_ip = self._maestro.get_ip_addr_priv()

        # -u for no buffering
        waitmaestro_sh = self._get_remote_files_path( 'waitmaestro.sh' )
        venv_python    = self._maestro.path_join( self._get_cloudsend_dir() , '.venv' , 'maestro' , 'bin' , 'python3' )

        cmd = "cd "+self._get_cloudsend_dir()+ " && "+waitmaestro_sh+" && sudo "+venv_python+" -u -m cloudsend.maestroclient " + maestro_command

        stdout , stderr = await self._exec_command(self.ssh_conn,cmd)

        async for line in proc.stdout:
            if not line:
                break
            self.debug(1,line,end='')

        # for l in await line_buffered(stdout):
        #     if not l:
        #         break
        #     self.debug(1,l,end='')
        
        # while True:
        #     outlines = stdout.readlines()
        #     if not outlines:
        #         errlines = stderr.readlines()
        #         for eline in errlines:
        #             self.debug(1,eline,end='')
        #         break
        #     for line in outlines:
        #         self.debug(1,line,end='')
        #     errlines = stderr.readlines()
        #     for eline in errlines:
        #         self.debug(1,eline,end='')

        # for l in line_buffered(stdout):
        #     if not l:
        #         errlines = stderr.readlines()
        #         for eline in errlines:
        #             self.debug(1,eline,end='')
        #         break
        #     self.debug(1,l,end='')
        #     errlines = stderr.readlines()
        #     for eline in errlines:
        #         self.debug(1,eline,end='')


    async def _install_maestro(self,reset):
        # this will block for some time the first time...
        self.debug_set_prefix(bcolors.BOLD+'INSTALLING MAESTRO: '+bcolors.ENDC)
        self._instances_states = dict()        
        self._start_and_update_instance(self._maestro)
        if reset:
            await self.reset_instance(self._maestro)
        await self._deploy_maestro(reset) # deploy the maestro now !
        self.debug_set_prefix(None)

    async def start(self,reset=False):
        # install maestro materials
        await self._install_maestro(reset)
        # triggers maestro::start
        await self._exec_maestro_command("start:"+str(reset))

    async def reset_instance(self,instance):
        self.debug(1,'RESETTING instance',instance.get_name())
        instanceid, ssh_conn , ftp_client = await self._wait_and_connect(instance)
        if ssh_conn is not None:
            await self.sftp_put_remote_file(ftp_client,'resetmaestro.sh')
            resetmaestro_sh = self._maestro.path_join( self._maestro.get_home_dir() , 'resetmaestro.sh' )
            commands = [
               { 'cmd' : 'chmod +x '+resetmaestro_sh+' && ' + resetmaestro_sh , 'out' : True }
            ]
            await self._run_ssh_commands(instance,ssh_conn,commands)
            #ftp_client.close()
            ssh_conn.close()
        self.debug(1,'RESETTING done')

    async def assign(self):
        # triggers maestro::assign
        await self._exec_maestro_command("allocate")

    async def deploy(self):
        # triggers maestro::deploy
        await self._exec_maestro_command("deploy") # use output - the deploy part will be skipped depending on option ...

    async def run(self):
        # triggers maestro::run
        await self._exec_maestro_command("run")

    async def watch(self,processes=None,daemon=True):
        # triggers maestro::wait_for_jobs_state
        await self._exec_maestro_command("watch:"+str(daemon))

    async def wakeup(self):
        # triggers maestro::wakeup
        await self._exec_maestro_command("wakeup")

    async def wait_for_jobs_state(self,job_state,processes=None):
        # triggers maestro::wait_for_jobs_state
        await self._exec_maestro_command("wait")

    async def get_jobs_states(self,processes=None):
        # triggers maestro::get_jobs_states
        await self._exec_maestro_command("get_states")

    async def print_jobs_summary(self,instance=None):
        # triggers maestro::print_jobs_summary
        await self._exec_maestro_command("print_summary")

    async def print_aborted_logs(self,instance=None):
        # triggers maestro::print_aborted_logs
        await self._exec_maestro_command("print_aborted")

    async def fetch_results(self,out_dir,processes=None):

        try:
            #os.rmdir(out_dir)
            shutil.rmtree(out_dir, ignore_errors=True)
        except:
            pass
        try:
            os.makedirs(out_dir)
        except:
            pass

        randnum          = str(random.randrange(1000))
        homedir          = self._maestro.get_home_dir()
        maestro_dir      = self._maestro.path_join( homedir , "cloudsend_tmp_fetch" + randnum )
        maestro_tar_file = "maestro"+randnum+".tar"
        maestro_tar_path = self._maestro.path_join( homedir , maestro_tar_file )

        # fetch the results on the maestro
        await self._exec_maestro_command("fetch_results:"+maestro_dir)

        # get the tar file of the results
        instanceid , ssh_conn , ftp_client = await self._wait_and_connect(self._maestro)
        stdout,stderr = await self._exec_command(ssh_conn,"cd " + maestro_dir + " && tar -cvf "+maestro_tar_path+" .")      
        self.debug(1,stdout.read()) #blocks
        self.debug(2,stderr.read()) #blocks
        local_tar_path = os.path.join(out_dir,maestro_tar_file)
        with open(local_tar_path,'wb') as outfile:
            await ftp_client.chdir( homedir )
            await ftp_client.get( maestro_tar_file , outfile )

        # untar
        os.system("tar -xvf "+local_tar_path+" -C "+out_dir)

        # cleanup
        os.remove(local_tar_path)
        stdout,stderr = await self._exec_command(ssh_conn,"rm -rf "+maestro_dir+" "+maestro_tar_path)

        # close
        #ftp_client.close()
        ssh_conn.close()

    def _get_or_create_instance(self,instance):
        instance , created = super()._get_or_create_instance(instance)
        # dangerous !
        #self.add_maestro_security_group(instance)
        # open web server

        return instance , created 

    @abstractmethod
    def grant_admin_rights(self,instance):
        pass

    @abstractmethod
    def add_maestro_security_group(self,instance):
        pass

    @abstractmethod
    def setup_auto_stop(self,instance):
        pass

    # needed by CloudSendProvider::_wait_for_instance 
    def serialize_state(self):
        pass


