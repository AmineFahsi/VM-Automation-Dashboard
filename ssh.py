import paramiko


class SSHManager:
    def __init__(self):
        self.host     = "192.168.0.3"
        self.user     = "root"
        self.password = "LHMguest@@2026"

    def run(self, command):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, username=self.user, password=self.password)

        stdin, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode().strip()
        error  = stderr.read().decode().strip()
        client.close()


        combined = "\n".join(filter(None, [output, error]))
        return combined