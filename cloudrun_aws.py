import boto3
import os , json
import cloudrunutils
from cloudrun import CloudRunError , CloudRunCommandState , CloudRunInstance , CloudRunEnvironment , CloudRunJobRuntimeInfo, CloudRunProvider
from cloudrun import cr_keypairName , cr_secGroupName , cr_bucketName , cr_vpcName , init_instance_name
from botocore.exceptions import ClientError
from datetime import datetime , timedelta
from botocore.config import Config
import asyncio
import re

def aws_get_config(region):
    if region is None:
        return Config()
    else:
        return Config(region_name=region)

def aws_create_keypair(region):
    self.debug(1,"Creating KEYPAIR ...")
    ec2_client = boto3.client("ec2",config=get_config(region))
    ec2 = boto3.resource('ec2',config=get_config(region))

    keypair = None
    try:
        keypair = ec2_client.create_key_pair(
            KeyName=cr_keypairName,
            DryRun=True,
            KeyType='rsa',
            KeyFormat='pem'
        )
    except ClientError as e: #DryRun=True >> always an "error"
        errmsg = str(e)
        
        if 'UnauthorizedOperation' in errmsg:
            self.debug(1,"The account is not authorized to create a keypair, please specify an existing keypair in the configuration or add Administrator privileges to the account")
            keypair = None
            #sys.exit()
            raise CloudRunError()

        elif 'DryRunOperation' in errmsg: # we are good with credentials
            try :
                keypair = ec2_client.create_key_pair(
                    KeyName=cr_keypairName,
                    DryRun=False,
                    KeyType='rsa',
                    KeyFormat='pem'
                )
                if region is None:
                    # get the default user region so we know what were getting ... (if it changes later etc... could be a mess)
                    my_session = boto3.session.Session()
                    region = my_session.region_name                
                fpath = "cloudrun-"+str(region)+".pem"
                pemfile = open(fpath, "w")
                pemfile.write(keypair['KeyMaterial']) # save the private key in the directory (we will use it with paramiko)
                pemfile.close()
                os.chmod(fpath, 0o600) # change permission to use with ssh (for debugging)
                self.debug(2,keypair)

            except ClientError as e2: # the keypair probably exists already
                errmsg2 = str(e2)
                if 'InvalidKeyPair.Duplicate' in errmsg2:
                    #keypair = ec2.KeyPair(cr_keypairName)
                    keypairs = ec2_client.describe_key_pairs( KeyNames= [cr_keypairName] )
                    keypair  = keypairs['KeyPairs'][0] 
                    self.debug(2,keypair)
                else:
                    self.debug(1,"An unknown error occured while retrieving the KeyPair")
                    self.debug(2,errmsg2)
                    #sys.exit()
                    raise CloudRunError()

        
        else:
            self.debug(1,"An unknown error occured while creating the KeyPair")
            self.debug(2,errmsg)
            #sys.exit()
            raise CloudRunError()
    
    return keypair 

def aws_find_or_create_default_vpc(region):
    ec2_client = boto3.client("ec2", config=get_config(region))
    vpcs = ec2_client.describe_vpcs(
        Filters=[
            {
                'Name': 'is-default',
                'Values': [
                    'true',
                ]
            },
        ]
    )
    defaultvpc = ec2_client.create_default_vpc() if len(vpcs['Vpcs'])==0 else vpcs['Vpcs'][0] 
    return defaultvpc

def aws_create_vpc(region,cloudId=None):
    self.debug(1,"Creating VPC ...")
    vpc = None
    ec2_client = boto3.client("ec2", config=get_config(region))

    if cloudId is not None:
        vpcID = cloudId
        try :
            vpcs = ec2_client.describe_vpcs(
                VpcIds=[
                    vpcID,
                ]
            )
            vpc = vpcs['Vpcs'][0]
        except ClientError as ce:
            ceMsg = str(ce)
            if 'InvalidVpcID.NotFound' in ceMsg:
                self.debug(1,"WARNING: using default VPC. "+vpcID+" is unavailable")
                vpc = find_or_create_default_vpc(region) 
            else:
                self.debug(1,"WARNING: using default VPC. Unknown error")
                vpc = find_or_create_default_vpc(region) 

    else:
        self.debug(1,"using default VPC (no VPC ID specified in config)")
        vpc = find_or_create_default_vpc(region)
    self.debug(2,vpc)

    return vpc 

def aws_create_security_group(region,vpc):
    self.debug(1,"Creating SECURITY GROUP ...")
    secGroup = None
    ec2_client = boto3.client("ec2", config=get_config(region))

    secgroups = ec2_client.describe_security_groups(Filters=[
        {
            'Name': 'group-name',
            'Values': [
                cr_secGroupName,
            ]
        },
    ])
    if len(secgroups['SecurityGroups']) == 0: # we have no security group, lets create one
        self.debug(1,"Creating new security group")
        secGroup = ec2_client.create_security_group(
            VpcId = vpc['VpcId'] ,
            Description = 'Allow SSH connection' ,
            GroupName = cr_secGroupName 
        )

        data = ec2_client.authorize_security_group_ingress(
            GroupId=secGroup['GroupId'],
            IpPermissions=[
                {'IpProtocol': 'tcp',
                'FromPort': 80,
                'ToPort': 80,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp',
                'FromPort': 22,
                'ToPort': 22,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
            ])
        self.debug(1,'Ingress Successfully Set %s' % data)

    else:
        secGroup = secgroups['SecurityGroups'][0]
    
    if secGroup is None:
        self.debug(1,"An unknown error occured while creating the security group")
        #sys.exit()
        raise CloudRunError()
    
    self.debug(2,secGroup) 

    return secGroup

# for now just return the first subnet present in the Vpc ...
def aws_create_subnet(region,vpc):
    self.debug(1,"Creating SUBNET ...")
    ec2 = boto3.resource('ec2',config=get_config(region))
    ec2_client = boto3.client("ec2", config=get_config(region))
    vpc_obj = ec2.Vpc(vpc['VpcId'])
    for subnet in vpc_obj.subnets.all():
        subnets = ec2_client.describe_subnets(SubnetIds=[subnet.id])
        subnet = subnets['Subnets'][0]
        self.debug(2,subnet)
        return subnet
    return None # todo: create a subnet

def aws_create_bucket(region):

    self.debug(1,"Creating BUCKET ...")

    s3_client = boto3.client('s3', config=get_config(region))
    s3 = boto3.resource('s3', config=get_config(region))

    try :

        bucket = s3_client.create_bucket(
            ACL='private',
            Bucket=cr_bucketName,
            CreateBucketConfiguration={
                'LocationConstraint': region
            },

        )

    except ClientError as e:
        errMsg = str(e)
        if 'BucketAlreadyExists' in errMsg:
            bucket = s3.Bucket(cr_bucketName)
        elif 'BucketAlreadyOwnedByYou' in errMsg:
            bucket = s3.Bucket(cr_bucketName)

    self.debug(2,bucket)

    return bucket 


def aws_upload_file( region , bucket , file_path ):
    self.debug(1,"uploading FILE ...")
    s3_client = boto3.client('s3', config=get_config(region))
    response = s3_client.upload_file( file_path, bucket['BucketName'], 'cr-run-script' )
    self.debug(2,response)

def aws_find_instance(instance_config):

    self.debug(1,"Searching INSTANCE ...")

    instanceName = init_instance_name(instance_config)
    region = instance_config['region']

    ec2_client = boto3.client("ec2", config=get_config(region))

    existing = ec2_client.describe_instances(
        Filters = [
            {
                'Name': 'tag:Name',
                'Values': [
                    instanceName
                ]
            },
            {
                'Name': 'instance-state-name' ,
                'Values' : [ # everything but 'terminated' and 'shutting down' ?
                    'pending' , 'running' , 'stopping' , 'stopped'
                ]
            }
        ]
    )

    if len(existing['Reservations']) > 0 and len(existing['Reservations'][0]['Instances']) >0 :
        instance = existing['Reservations'][0]['Instances'][0]
        self.debug(1,"Found exisiting instance !",instance['InstanceId'])
        self.debug(2,instance)
        instance = CloudRunInstance( region , instanceName , instance['InstanceId'] , instance_config, instance )
        return instance 
    
    else:

        self.debug(1,"not found")

        return None 


def aws_create_instance(instance_config,vpc,subnet,secGroup):

    self.debug(1,"Creating INSTANCE ...")

    instanceName = init_instance_name(instance_config)

    region = instance_config['region']

    ec2_client = boto3.client("ec2", config=get_config(region))

    existing = ec2_client.describe_instances(
        Filters = [
            {
                'Name': 'tag:Name',
                'Values': [
                    instanceName
                ]
            },
            {
                'Name': 'instance-state-name' ,
                'Values' : [ # everything but 'terminated' and 'shutting down' ?
                    'pending' , 'running' , 'stopping' , 'stopped'
                ]
            }
        ]
    )

    #print(existing)

    created = False

    if len(existing['Reservations']) > 0 and len(existing['Reservations'][0]['Instances']) >0 :
        instance = existing['Reservations'][0]['Instances'][0]
        self.debug(1,"Found exisiting instance !",instance['InstanceId'])
        self.debug(2,instance)
        instance = CloudRunInstance( region , instanceName , instance['InstanceId'] , instance_config, instance )
        return instance , created

    if instance_config.get('cpus') is not None:
        cpus_spec = {
            'CoreCount': instance_config['cpus'],
            'ThreadsPerCore': 1
        },
    else:
        cpus_spec = { }  

    if instance_config.get('gpu'):
        gpu_spec = [ { 'Type' : instance_config['gpu'] } ]
    else:
        gpu_spec = [ ]
    
    if instance_config.get('eco') :
        market_options = {
            'MarketType': 'spot',
            'SpotOptions': {
                'SpotInstanceType': 'persistent', #one-time'|'persistent',
                'InstanceInterruptionBehavior': 'stop' #'hibernate'|'stop'|'terminate'
            }
        }     
        if 'eco_life' in instance_config and isinstance(instance_config['eco_life'],timedelta):
            validuntil = datetime.today() + instance_config['eco_life']
            market_options['SpotOptions']['ValidUntil'] = validuntil
        if 'max_bid' in instance_config and isinstance(instance_config['max_bid'],str):
            market_options['SpotOptions']['MaxPrice'] = instance_config['max_bid']
    else:
        market_options = { } 

    if instance_config.get('disk_size'):
        #TODO: improve selection of disk_type
        disk_size = instance_config['disk_size']
        if disk_size < 1024:
            volume_type = 'standard'
        else:
            volume_type = 'st1' # sc1, io1, io2, gp2, gp3
        if 'disk_type' in instance_config and instance_config['disk_type']:
            volume_type = instance_config['disk_type']
        block_device_mapping = [
            {
                'DeviceName' : '/dev/sda' ,
                'Ebs' : {
                    "DeleteOnTermination" : True ,
                    "VolumeSize": disk_size ,
                    "VolumeType": volume_type
                }
            }
        ]
    else:
        block_device_mapping = [ ]

    try:

        instances = ec2_client.run_instances(
                ImageId = instance_config['img_id'],
                MinCount = 1,
                MaxCount = 1,
                InstanceType = instance_config['size'],
                KeyName = cr_keypairName,
                SecurityGroupIds=[secGroup['GroupId']],
                SubnetId = subnet['SubnetId'],
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [
                            {
                                'Key': 'Name',
                                'Value': instanceName
                            },
                        ]
                    },
                ],
                ElasticGpuSpecification = gpu_spec ,
                InstanceMarketOptions = market_options,
                CpuOptions = cpus_spec,
                HibernationOptions={
                    'Configured': False
                },            
        )    
    except ClientError as ce:
        errmsg = str(ce)
        if 'InsufficientInstanceCapacity' in errmsg: # Amazon doesnt have enough resources at the moment
            self.debug(1,"AWS doesnt have enough SPOT resources at the moment, retrying in 2 minutes ...",errmsg)
        else:
            self.debug(1,"An error occured while trying to create this instance",errmsg)
        raise CloudRunError()


    created = True

    self.debug(2,instances["Instances"][0])

    instance = CloudRunInstance( region , instanceName , instances["Instances"][0]["InstanceId"] , instance_config, instances["Instances"][0] )

    return instance , created

def aws_create_instance_objects(instance_config):
    region   = instance_config['region']
    keypair  = aws_create_keypair(region)
    vpc      = aws_create_vpc(region,instance_config.get('cloud_id')) 
    secGroup = aws_create_security_group(region,vpc)
    subnet   = aws_create_subnet(region,vpc) 
    # this is where all the instance_config is actually used
    instance , created = aws_create_instance(instance_config,vpc,subnet,secGroup)

    return instance , created 


def aws_start_instance(instance):
    ec2_client = boto3.client("ec2", config=get_config(instance.get_region()))

    try:
        ec2_client.start_instances(InstanceIds=[instance.get_id()])
    except ClientError as botoerr:
        errmsg = str(botoerr)
        if 'IncorrectSpotRequestState' in errmsg:
            self.debug(1,"Could not start because it is a SPOT instance, waiting on SPOT ...",errmsg)
            # try reboot
            try:
                ec2_client.reboot_instances(InstanceIds=[instance.get_id()])
            except ClientError as botoerr2:
                errmsg2 = str(botoerr2)
                if 'IncorrectSpotRequestState' in errmsg2:
                    self.debug(1,"Could not reboot instance because of SPOT ... ",errmsg2)
                    raise CloudRunError()
                    # terminate_instance(config,instance)
                
        else:
            self.debug(2,botoerr)
            raise CloudRunError()

def aws_stop_instance(instance):
    ec2_client = boto3.client("ec2", config=get_config(instance.get_region()))

    ec2_client.stop_instances(InstanceIds=[instance.get_id()])

def aws_terminate_instance(instance):

    ec2_client = boto3.client("ec2", config=get_config(instance.get_region()))

    ec2_client.terminate_instances(InstanceIds=[instance.get_id()])

    if instance.get_data('SpotInstanceRequestId'):

        ec2_client.cancel_spot_instance_requests(SpotInstanceRequestIds=[instance.get_data('SpotInstanceRequestId')]) 

def aws_get_instance_info(instance):
    region = instance.get_region()
    ec2_client   = boto3.client("ec2", config=get_config(region))
    instances    = ec2_client.describe_instances( InstanceIds=[instance.get_id()] )
    instance_new = instances['Reservations'][0]['Instances'][0]

    instance = CloudRunInstance( region , instance.get_name() , instance.get_id() , instance_config, instance_new )
    instance.set_dns_addr(instance_new.get('PublicDnsName'))
    instance.set_ip_addr(instance_new.get('PublicIpAddress'))
    instance.set_state(instance_new.get('State').get('Name'))

    return instance

def line_buffered(f):
    line_buf = ""
    doContinue = True
    try :
        while doContinue and not f.channel.exit_status_ready():
            try:
                line_buf += f.read(16).decode("utf-8")
                if line_buf.endswith('\n'):
                    yield line_buf
                    line_buf = ''
            except Exception as e:
                #errmsg = str(e)
                #self.debug(1,"error (1) while buffering line",errmsg)
                pass 
                #doContinue = False
    except Exception as e0:
        self.debug(1,"error (2) while buffering line",str(e0))
        #doContinue = False

##########
# PUBLIC #
##########

class AWSCloudRunProvider(CloudRunProvider):

    def __init__(self, conf):
        CloudRunProvider.__init__(self,conf)

    def get_instance(self):

        return find_instance(self.config)

    def start_instance(self):

        instance = find_instance(self.config)

        if instance is None:
            instance , created = create_instance_objects(self.config)
        else:
            created = False

        return instance , created

    def stop_instance(self):
        stop_instance(self.get_instance())

    def terminate_instance(self):
        terminate_instance(self.get_instance())

    def get_user_region(self):
        my_session = boto3.session.Session()
        region = my_session.region_name     
        return region         

