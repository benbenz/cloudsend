from cloudsend import provider as cs
import asyncio , os , sys
from cloudsend.core import CloudSendProcessState
import traceback
import json

async def tail_loop(script_hash,uid):

    generator = await cs_client.tail(script_hash,uid) 
    for line in generator:
        print(line)


async def mainloop(cs_client,reset=False):

    print("\n== START ==\n")

    # distribute the jobs on the instances (dummy algo for now)
    await cs_client.start(reset) 

    print("\n== DEPLOY ==\n")

    # pre-deploy instance , environments and job files
    # it is recommended to wait here allthough run.sh should wait for bootstraping
    # currently, the bootstraping is non-blocking
    # so this will barely wait ... (the jobs will do the waiting ...)
    await cs_client.deploy()

    print("\n== RUN ==\n")

    # run the scripts and get a process back
    processes = await cs_client.run()

    print("\n== WAIT ==\n")

    print("Waiting for DONE or ABORTED ...")
    # now that we have 'watch' before 'wait' , this will exit instantaneously
    # because watch includes 'wait' mode intrinsiquely
    processes = await cs_client.wait(CloudSendProcessState.DONE|CloudSendProcessState.ABORTED,processes)

    print("\n== SUMMARY ==\n")

    # just to show the API ...
    await cs_client.get_jobs_states(processes)

    # print("\n== WAIT and TAIL ==\n")

    await cs_client.print_aborted_logs()

    print("\n== FETCH RESULTS ==\n")

    await cs_client.fetch_results(os.path.join(os.getcwd(),'tmp'))

    print("\n== DONE ==\n")

async def waitloop(cs_client):

    print("\n== START ==\n")

    await cs_client.start()

    print("\n== WAIT ==\n")
    
    await cs_client.wait(CloudSendProcessState.DONE|CloudSendProcessState.ABORTED)

    print("\n== SUMMARY ==\n")

    # just to show the API ...
    await cs_client.get_jobs_states()
    await cs_client.print_aborted_logs()

    print("\n== FETCH RESULTS ==\n")

    await cs_client.fetch_results(os.path.join(os.getcwd(),'tmp'))

    print("\n== DONE ==\n")        

# run main loop
def main():

    if len(sys.argv)<=1:
        print("You need to specify a config file path: config.json or config.py")
        sys.exit(1)
    
    config_file = sys.argv[1]
    cs_client   = cs.get_client(config_file)
    command = None
    if len(sys.argv)>2: 
        command = sys.argv[2]
    
    if command == 'wait':
        asyncio.run( waitloop(cs_client) )
    elif command == 'reset':
        asyncio.run( mainloop(cs_client,True) )
    else:
        asyncio.run( mainloop(cs_client) )

if __name__ == '__main__':
    main()    