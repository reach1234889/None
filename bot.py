import discord
from discord.ext import commands
from discord import ui
import os
import random
import string
import json
import subprocess
from dotenv import load_dotenv
import asyncio
import datetime
import docker
import time

# Load environment variables
load_dotenv()

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
VPS_STORAGE_FILE = 'vps_data.json'
ADMIN_ROLE_ID = 1376177459870961694  # Admin role ID

# Initialize bot with command prefix '!'
class CustomBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_command = None
        self._last_command_time = 0

    async def process_commands(self, message):
        if message.author.bot:
            return

        current_time = time.time()
        if (self._last_command == message.content and 
            current_time - self._last_command_time < 2):
            return

        self._last_command = message.content
        self._last_command_time = current_time
        await super().process_commands(message)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = CustomBot(command_prefix='!', intents=intents)

# Initialize Docker client
try:
    client = docker.from_env()
except Exception as e:
    print(f"Failed to initialize Docker client: {e}")
    client = None

# Store VPS data
vps_data = {}

def load_vps_data():
    global vps_data
    if os.path.exists(VPS_STORAGE_FILE):
        with open(VPS_STORAGE_FILE, 'r') as f:
            vps_data = json.load(f)
            # Update existing VPS data to include vps_id if missing
            for token, data in vps_data.items():
                if 'vps_id' not in data:
                    data['vps_id'] = generate_vps_id()
            save_vps_data()

def save_vps_data():
    with open(VPS_STORAGE_FILE, 'w') as f:
        json.dump(vps_data, f)

def generate_token():
    """Generate a random token for VPS access"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

def generate_vps_id():
    """Generate a unique VPS ID"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def has_required_role(ctx):
    """Check if user has required role to use bot commands"""
    # Allow all users to use basic commands
    return True

def has_admin_role(ctx):
    """Check if user has admin role"""
    return any(role.id == ADMIN_ROLE_ID for role in ctx.author.roles)

async def capture_ssh_session_line(process):
    try:
        while True:
            output = await process.stdout.readline()
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output:
                return output.split("ssh session:")[1].strip()
        return None
    except Exception as e:
        print(f"Error capturing SSH session: {e}")
        return None

def count_user_servers(userid):
    count = 0
    for token, data in vps_data.items():
        if data["created_by"] == userid:
            count += 1
    return count

async def run_docker_command(container_id, command, timeout=120):
    """Run a Docker command asynchronously with timeout"""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                raise Exception(f"Command failed: {stderr.decode()}")
            return True
        except asyncio.TimeoutError:
            process.kill()
            raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        print(f"Error running Docker command: {e}")
        return False

async def kill_apt_processes(container_id):
    """Kill any running apt processes"""
    try:
        await run_docker_command(container_id, ["bash", "-c", "killall apt apt-get dpkg || true"])
        await asyncio.sleep(2)
        await run_docker_command(container_id, ["bash", "-c", "rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*"])
        await asyncio.sleep(2)
        return True
    except Exception as e:
        print(f"Error killing apt processes: {e}")
        return False

async def wait_for_apt_lock(container_id, status_msg):
    """Wait for apt lock to be released"""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            # First try to kill any running apt processes
            await kill_apt_processes(container_id)
            
            # Check if lock exists
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "bash", "-c", "lsof /var/lib/dpkg/lock-frontend",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:  # No lock found
                return True
                
            await status_msg.edit(content=f"🔄 Waiting for package manager to be ready... (Attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Error checking apt lock: {e}")
            await asyncio.sleep(5)
    
    return False

async def setup_container(container_id, status_msg, memory):
    """Basic container setup"""
    try:
        # Ensure container is running
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🔍 Checking container status...", ephemeral=True)
        else:
            await status_msg.edit(content="🔍 Checking container status...")
            
        container = client.containers.get(container_id)
        if container.status != "running":
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send("🚀 Starting container...", ephemeral=True)
            else:
                await status_msg.edit(content="🚀 Starting container...")
            container.start()
            await asyncio.sleep(5)  # Wait for container to fully start

        # Install tmate
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("📦 Installing tmate...", ephemeral=True)
        else:
            await status_msg.edit(content="📦 Installing tmate...")
            
        # Update package list
        update_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "apt-get", "update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await update_process.communicate()
        if update_process.returncode != 0:
            raise Exception(f"Failed to update package list: {stderr.decode()}")

        # Install tmate
        install_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "apt-get", "install", "-y", "tmate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await install_process.communicate()
        if install_process.returncode != 0:
            raise Exception(f"Failed to install tmate: {stderr.decode()}")

        # Set hostname
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🎨 Setting basic configuration...", ephemeral=True)
        else:
            await status_msg.edit(content="🎨 Setting basic configuration...")
            
        hostname_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", "echo 'thunderhost' > /etc/hostname && hostname thunderhost",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await hostname_process.communicate()
        if hostname_process.returncode != 0:
            raise Exception(f"Failed to set hostname: {stderr.decode()}")

        # Set memory limit in cgroup
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("⚙️ Setting resource limits...", ephemeral=True)
        else:
            await status_msg.edit(content="⚙️ Setting resource limits...")
            
        memory_bytes = memory * 1024 * 1024 * 1024  # Convert GB to bytes
        memory_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", f"echo {memory_bytes} > /sys/fs/cgroup/memory.max",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await memory_process.communicate()
        if memory_process.returncode != 0:
            print(f"Warning: Could not set memory limit in cgroup: {stderr.decode()}")

        # Set model information
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("⚙️ Setting system information...", ephemeral=True)
        else:
            await status_msg.edit(content="⚙️ Setting system information...")
            
        machine_info_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", "echo 'ThunderHost' > /etc/machine-info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await machine_info_process.communicate()
        if machine_info_process.returncode != 0:
            print(f"Warning: Could not set machine info: {stderr.decode()}")

        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("✅ Container setup completed successfully!", ephemeral=True)
        else:
            await status_msg.edit(content="✅ Container setup completed successfully!")
            
        return True
    except Exception as e:
        error_msg = f"Setup failed: {str(e)}"
        print(error_msg)
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send(f"❌ {error_msg}", ephemeral=True)
        else:
            await status_msg.edit(content=f"❌ {error_msg}")
        return False

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    load_vps_data()

@bot.command(name='commands')
@commands.check(has_required_role)
async def show_commands(ctx):
    """Show all available commands"""
    embed = discord.Embed(title="🤖 ThunderHost Bot Commands", color=discord.Color.blue())
    
    # User commands
    embed.add_field(name="User Commands", value="""
`!create_vps <memory> <cpu> <disk> <username>` - Create a new VPS
`!connect_vps <token>` - Connect to your VPS
`!list` - List all your VPS instances
`!commands` - Show this help message
`!manage_vps <vps_id>` - You can manage your VPS from there
`!transfer_vps <vps_id> <@user>` - Transfer VPS ownership to another user
""", inline=False)
    
    # Admin commands
    if has_admin_role(ctx):
        embed.add_field(name="Admin Commands", value="""
`!vps_list` - List all VPS instances with user information
`!delete_vps <vps_id> <username>` - Delete a VPS instance
""", inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='create_vps')
@commands.check(has_admin_role)
async def create_vps_command(ctx, memory: int, cpu: int, disk: int, owner: discord.Member):
    """Create a new VPS with specified parameters (Admin only)
    Usage: !create_vps <memory> <cpu> <disk> @user
    Creates a VPS for the mentioned user"""
    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server!")
        return

    if not client:
        await ctx.send("❌ Docker is not available. Please contact the administrator.")
        return

    try:
        # Send initial message
        status_msg = await ctx.send("🚀 Creating VPS instance... This may take a few minutes.")

        # Generate VPS ID
        vps_id = generate_vps_id()

        # Calculate memory limit in bytes
        memory_bytes = memory * 1024 * 1024 * 1024  # Convert GB to bytes

        # Create Docker container with a proper entrypoint
        await status_msg.edit(content="⚙️ Initializing container...")
        container = client.containers.run(
            "ubuntu:22.04",
            detach=True,
            privileged=True,
            hostname="thunderhost",
            mem_limit=memory_bytes,
            cpu_period=100000,
            cpu_quota=int(cpu * 100000),
            cap_add=["ALL"],
            command="tail -f /dev/null",  # Keep container running
            tty=True
        )

        # Wait for container to be ready
        await status_msg.edit(content="🔧 Container created. Setting up environment...")
        await asyncio.sleep(5)

        # Setup container
        if not await setup_container(container.id, status_msg, memory):
            raise Exception("Failed to setup container")

        await status_msg.edit(content="🔐 Starting SSH session...")

        # Start tmate session
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container.id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if not ssh_session_line:
            raise Exception("Failed to get tmate session")

        # Generate access token
        token = generate_token()
        
        # Store VPS data
        vps_data[token] = {
            "vps_id": vps_id,
            "container_id": container.id,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "username": owner.name,  # Use the owner's name as username
            "created_by": str(owner.id),
            "created_at": str(datetime.datetime.now()),
            "tmate_session": ssh_session_line
        }
        save_vps_data()
        
        # Send VPS information to the owner's DM
        try:
            embed = discord.Embed(title="🎉 VPS Creation Successful", color=discord.Color.green())
            embed.add_field(name="🆔 VPS ID", value=vps_id, inline=True)
            embed.add_field(name="💾 Memory", value=f"{memory}GB", inline=True)
            embed.add_field(name="⚡ CPU", value=f"{cpu} cores", inline=True)
            embed.add_field(name="💿 Disk", value=f"{disk}GB", inline=True)
            embed.add_field(name="👤 Username", value=owner.name, inline=True)
            embed.add_field(name="🔑 Access Token", value=token, inline=False)
            embed.add_field(name="🔒 Tmate Session", value=f"```{ssh_session_line}```", inline=False)
            embed.add_field(name="ℹ️ Note", value="This is a basic VPS instance. You can install and configure additional packages as needed.", inline=False)
            
            await owner.send(embed=embed)
            await status_msg.edit(content=f"✅ VPS creation successful! VPS has been created for {owner.mention}. Check your DMs for connection details.")
        except discord.Forbidden:
            await status_msg.edit(content=f"❌ I couldn't send a DM to {owner.mention}. Please ask them to enable DMs from server members.")
            
    except Exception as e:
        error_msg = f"❌ An error occurred while creating the VPS: {str(e)}"
        print(error_msg)
        await ctx.send(error_msg)
        if 'container' in locals():
            try:
                container.remove(force=True)
            except:
                pass

@bot.command(name='list')
@commands.check(has_required_role)
async def list_vps(ctx):
    """List all VPS instances owned by the user"""
    try:
        user_vps = [data for data in vps_data.values() if data["created_by"] == str(ctx.author.id)]
        
        if not user_vps:
            await ctx.send("You don't have any VPS instances.")
            return

        embed = discord.Embed(title="Your VPS Instances", color=discord.Color.blue())
        
        for vps in user_vps:
            # Check if container is still running
            try:
                container = client.containers.get(vps["container_id"])
                status = "🟢 Running" if container.status == "running" else "🔴 Stopped"
            except:
                status = "🔴 Not Found"

            embed.add_field(
                name=f"VPS {vps.get('vps_id', 'Unknown')}",
                value=f"""
Status: {status}
Memory: {vps['memory']}GB
CPU: {vps['cpu']} cores
Disk: {vps['disk']}GB
Username: {vps['username']}
Created: {vps['created_at']}
""",
                inline=False
            )
        
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error listing VPS instances: {str(e)}")

@bot.command(name='vps_list')
@commands.check(has_admin_role)
async def admin_list_vps(ctx):
    """List all VPS instances (Admin only)"""
    try:
        if not vps_data:
            await ctx.send("No VPS instances found.")
            return

        embed = discord.Embed(title="All VPS Instances", color=discord.Color.blue())
        valid_vps_count = 0
        vps_to_remove = []  # Store tokens to remove after iteration
        
        # Create a copy of the keys to iterate over
        for token in list(vps_data.keys()):
            vps = vps_data[token]
            try:
                # Get user information
                try:
                    user = await bot.fetch_user(int(vps.get("created_by", "0")))
                    username = user.name
                except:
                    username = "Unknown User"

                # Check container status
                try:
                    container = client.containers.get(vps.get("container_id", ""))
                    status = "🟢 Running" if container.status == "running" else "🔴 Stopped"
                except:
                    # Mark for removal after iteration
                    vps_to_remove.append(token)
                    continue

                # Get VPS information with safe defaults
                vps_info = f"""
Owner: {username}
Status: {status}
Memory: {vps.get('memory', 'Unknown')}GB
CPU: {vps.get('cpu', 'Unknown')} cores
Disk: {vps.get('disk', 'Unknown')}GB
Username: {vps.get('username', 'Unknown')}
Created: {vps.get('created_at', 'Unknown')}
VPS ID: {vps.get('vps_id', 'Unknown')}
"""

                embed.add_field(
                    name=f"VPS {vps.get('vps_id', 'Unknown')}",
                    value=vps_info,
                    inline=False
                )
                valid_vps_count += 1
            except Exception as e:
                print(f"Error processing VPS {token}: {e}")
                continue
        
        # Remove invalid VPS entries after iteration
        for token in vps_to_remove:
            del vps_data[token]
        if vps_to_remove:
            save_vps_data()
        
        if valid_vps_count == 0:
            await ctx.send("No valid VPS instances found.")
            return

        # Add a footer with the total count
        embed.set_footer(text=f"Total VPS instances: {valid_vps_count}")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error listing VPS instances: {str(e)}")
        print(f"Detailed error: {e}")

@bot.command(name='delete_vps')
@commands.check(has_admin_role)
async def delete_vps(ctx, vps_id: str, username: str):
    """Delete a VPS instance (Admin only)"""
    try:
        # Find VPS by ID and username
        vps_to_delete = None
        token_to_delete = None
        
        for token, vps in vps_data.items():
            if vps.get('vps_id') == vps_id and vps['username'] == username:
                vps_to_delete = vps
                token_to_delete = token
                break
        
        if not vps_to_delete:
            await ctx.send("❌ VPS not found!")
            return
        
        # Stop and remove container
        try:
            container = client.containers.get(vps_to_delete["container_id"])
            container.stop()
            container.remove()
        except Exception as e:
            print(f"Warning: Error removing container: {e}")
        
        # Remove from storage
        del vps_data[token_to_delete]
        save_vps_data()
        
        await ctx.send(f"✅ VPS {vps_id} has been deleted successfully!")
    except Exception as e:
        await ctx.send(f"❌ Error deleting VPS: {str(e)}")

@bot.command(name='connect_vps')
@commands.check(has_required_role)
async def connect_vps(ctx, token: str):
    """Connect to a VPS using the provided token"""
    if token not in vps_data:
        await ctx.send("Invalid token!")
        return
        
    vps_info = vps_data[token]
    
    try:
        # Check if container exists and is running
        try:
            container = client.containers.get(vps_info["container_id"])
            if container.status != "running":
                container.start()
                await asyncio.sleep(5)  # Wait for container to fully start
        except:
            await ctx.send("VPS instance not found or is no longer available.")
            return

        # Regenerate tmate session
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", vps_info["container_id"], "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if not ssh_session_line:
            raise Exception("Failed to get tmate session")

        # Update stored session
        vps_info["tmate_session"] = ssh_session_line
        save_vps_data()
        
        # Send connection details to user's DM
        embed = discord.Embed(title="VPS Connection Details", color=discord.Color.blue())
        embed.add_field(name="Username", value=vps_info["username"], inline=True)
        embed.add_field(name="Tmate Session", value=f"```{ssh_session_line}```", inline=False)
        embed.add_field(name="Connection Instructions", value="1. Copy the Tmate session command\n2. Open your terminal\n3. Paste and run the command\n4. You will be connected to your VPS", inline=False)
        
        await ctx.author.send(embed=embed)
        await ctx.send("Connection details sent to your DMs! Use the Tmate command to connect to your VPS.")
        
    except discord.Forbidden:
        await ctx.send("I couldn't send you a DM. Please enable DMs from server members.")
    except Exception as e:
        await ctx.send(f"An error occurred while connecting to the VPS: {str(e)}")

@bot.command(name='check_ram')
@commands.check(has_required_role)
async def check_ram(ctx, vps_id: str):
    """Check RAM limit of a VPS"""
    try:
        # Find VPS by ID
        vps = None
        for data in vps_data.values():
            if data.get('vps_id') == vps_id and data["created_by"] == str(ctx.author.id):
                vps = data
                break
        
        if not vps:
            await ctx.send("❌ VPS not found or you don't have access to it!")
            return

        # Check container status
        try:
            container = client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running!")
                return

            # Get memory info
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", vps["container_id"], "bash", "-c", "free -h",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                raise Exception(f"Failed to get memory info: {stderr.decode()}")

            embed = discord.Embed(title=f"Memory Info for VPS {vps_id}", color=discord.Color.blue())
            embed.add_field(name="Memory Info", value=f"```{stdout.decode()}```", inline=False)
            embed.add_field(name="Configured Limit", value=f"{vps['memory']}GB", inline=True)
            
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error checking RAM: {str(e)}")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

# Error handler for missing role
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use this command!")
    else:
        print(f"Error: {error}")

class VPSManagementView(ui.View):
    def __init__(self, vps_data, vps_id, container_id):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.vps_data = vps_data
        self.vps_id = vps_id
        self.container_id = container_id
        self.client = docker.from_env()

    async def handle_missing_container(self, interaction: discord.Interaction):
        """Handle cases where the container no longer exists"""
        # Find and remove the VPS data
        for token, data in self.vps_data.items():
            if data.get('vps_id') == self.vps_id:
                del self.vps_data[token]
                save_vps_data()
                break
        
        # Update the original message
        embed = discord.Embed(title=f"VPS Management - {self.vps_id}", color=discord.Color.red())
        embed.add_field(name="Status", value="🔴 Container Not Found", inline=True)
        embed.add_field(name="Note", value="This VPS instance is no longer available. Please create a new one.", inline=False)
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("❌ This VPS instance is no longer available. Please create a new one.", ephemeral=True)

    @discord.ui.button(label="Start VPS", style=discord.ButtonStyle.green)
    async def start_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Acknowledge the interaction immediately
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = self.client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            if container.status == "running":
                await interaction.followup.send("VPS is already running!", ephemeral=True)
                return
            
            container.start()
            await asyncio.sleep(5)  # Wait for container to start
            
            # Update the original message
            embed = discord.Embed(title=f"VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            # Get VPS data
            vps = None
            for data in self.vps_data.values():
                if data.get('vps_id') == self.vps_id:
                    vps = data
                    break
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ VPS started successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error starting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Stop VPS", style=discord.ButtonStyle.red)
    async def stop_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Acknowledge the interaction immediately
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = self.client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            if container.status != "running":
                await interaction.followup.send("VPS is already stopped!", ephemeral=True)
                return
            
            container.stop()
            
            # Update the original message
            embed = discord.Embed(title=f"VPS Management - {self.vps_id}", color=discord.Color.orange())
            embed.add_field(name="Status", value="🔴 Stopped", inline=True)
            
            # Get VPS data
            vps = None
            for data in self.vps_data.values():
                if data.get('vps_id') == self.vps_id:
                    vps = data
                    break
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ VPS stopped successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error stopping VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Restart VPS", style=discord.ButtonStyle.blurple)
    async def restart_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Acknowledge the interaction immediately
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = self.client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            container.restart()
            await asyncio.sleep(5)  # Wait for container to restart
            
            # Update the original message
            embed = discord.Embed(title=f"VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            # Get VPS data
            vps = None
            for data in self.vps_data.values():
                if data.get('vps_id') == self.vps_id:
                    vps = data
                    break
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ VPS restarted successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error restarting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Reinstall OS", style=discord.ButtonStyle.grey)
    async def reinstall_os(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Check if container exists
            try:
                container = self.client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            # Create OS selection view
            view = OSSelectionView(self.vps_data, self.vps_id, self.container_id, interaction.message)
            await interaction.response.send_message("Select new OS:", view=view, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Transfer VPS", style=discord.ButtonStyle.grey)
    async def transfer_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TransferVPSModal(self.vps_id)
        await interaction.response.send_modal(modal)

class OSSelectionView(ui.View):
    def __init__(self, vps_data, vps_id, container_id, original_message):
        super().__init__(timeout=300)
        self.vps_data = vps_data
        self.vps_id = vps_id
        self.container_id = container_id
        self.client = docker.from_env()
        self.original_message = original_message
        
        # Add OS options
        self.add_os_button("Ubuntu 22.04", "ubuntu:22.04")
        self.add_os_button("Debian 12", "debian:12")
        self.add_os_button("Arch Linux", "archlinux:latest")
        self.add_os_button("Alpine", "alpine:latest")
        self.add_os_button("CentOS 7", "centos:7")
        self.add_os_button("Fedora 38", "fedora:38")

    def add_os_button(self, label: str, image: str):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.grey)
        
        async def os_callback(interaction: discord.Interaction):
            await self.reinstall_os(interaction, image)
        
        button.callback = os_callback
        self.add_item(button)

    async def reinstall_os(self, interaction: discord.Interaction, image: str):
        try:
            # Get VPS data
            vps = None
            for data in self.vps_data.values():
                if data.get('vps_id') == self.vps_id:
                    vps = data
                    break

            if not vps:
                await interaction.response.send_message("❌ VPS not found!", ephemeral=True)
                return

            # Acknowledge the interaction immediately
            await interaction.response.defer(ephemeral=True)

            # Stop and remove old container
            try:
                old_container = self.client.containers.get(self.container_id)
                old_container.stop()
                old_container.remove()
            except Exception as e:
                print(f"Warning: Error removing old container: {e}")

            # Send status update
            status_msg = await interaction.followup.send("🔄 Reinstalling VPS... This may take a few minutes.", ephemeral=True)
            
            # Calculate memory limit
            memory_bytes = vps['memory'] * 1024 * 1024 * 1024

            # Create new container
            try:
                container = self.client.containers.run(
                    image,
                    detach=True,
                    privileged=True,
                    hostname="thunderhost",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(vps['cpu'] * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True
                )
            except Exception as e:
                await status_msg.edit(content=f"❌ Failed to create container: {str(e)}")
                return

            # Update VPS data
            vps['container_id'] = container.id
            save_vps_data()

            # Setup container
            try:
                if not await setup_container(container.id, status_msg, vps['memory']):
                    raise Exception("Failed to setup container")
            except Exception as e:
                await status_msg.edit(content=f"❌ Container setup failed: {str(e)}")
                return

            # Start tmate session
            try:
                exec_cmd = await asyncio.create_subprocess_exec(
                    "docker", "exec", container.id, "tmate", "-F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                ssh_session_line = await capture_ssh_session_line(exec_cmd)
                if ssh_session_line:
                    vps['tmate_session'] = ssh_session_line
                    save_vps_data()
            except Exception as e:
                print(f"Warning: Failed to start tmate session: {e}")

            # Send success message
            await status_msg.edit(content="✅ VPS reinstalled successfully!")
            
            # Update the original message with new status
            try:
                embed = discord.Embed(title=f"VPS Management - {self.vps_id}", color=discord.Color.green())
                embed.add_field(name="Status", value="🟢 Running", inline=True)
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
                embed.add_field(name="OS", value=image, inline=True)
                
                await self.original_message.edit(embed=embed, view=None)
            except Exception as e:
                print(f"Warning: Failed to update original message: {e}")

        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Error reinstalling VPS: {str(e)}", ephemeral=True)
            except:
                # If all else fails, try to send a message to the channel
                try:
                    channel = interaction.channel
                    await channel.send(f"❌ Error reinstalling VPS {self.vps_id}: {str(e)}")
                except:
                    print(f"Failed to send error message: {e}")

    async def on_timeout(self):
        # Disable all buttons when the view times out
        for item in self.children:
            item.disabled = True
        try:
            await self.original_message.edit(view=self)
        except:
            pass

@bot.command(name='manage_vps')
@commands.check(has_required_role)
async def manage_vps(ctx, vps_id: str):
    """Manage a VPS instance"""
    try:
        # Find VPS by ID
        vps = None
        for data in vps_data.values():
            if data.get('vps_id') == vps_id and data["created_by"] == str(ctx.author.id):
                vps = data
                break
        
        if not vps:
            await ctx.send("❌ VPS not found or you don't have access to it!")
            return

        # Check container status
        try:
            container = client.containers.get(vps["container_id"])
            status = "🟢 Running" if container.status == "running" else "🔴 Stopped"
        except:
            status = "🔴 Not Found"

        # Create embed with VPS info
        embed = discord.Embed(title=f"VPS Management - {vps_id}", color=discord.Color.blue())
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
        embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
        embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
        embed.add_field(name="Username", value=vps['username'], inline=True)
        embed.add_field(name="Created", value=vps['created_at'], inline=True)

        # Create view with management buttons
        view = VPSManagementView(vps_data, vps_id, vps["container_id"])
        
        # Send message and store it for later updates
        message = await ctx.send(embed=embed, view=view)
        view.original_message = message
    except Exception as e:
        await ctx.send(f"❌ Error managing VPS: {str(e)}")

class TransferVPSModal(ui.Modal, title='Transfer VPS'):
    def __init__(self, vps_id: str):
        super().__init__()
        self.vps_id = vps_id
        self.new_owner = ui.TextInput(
            label='New Owner',
            placeholder='Enter user ID or @mention',
            required=True
        )
        self.add_item(self.new_owner)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Get the new owner input
            new_owner_input = self.new_owner.value.strip()
            
            # Handle @mention format
            if new_owner_input.startswith('<@') and new_owner_input.endswith('>'):
                new_owner_id = new_owner_input[2:-1]
            else:
                new_owner_id = new_owner_input

            # Find the VPS
            vps = None
            old_token = None
            for token, data in vps_data.items():
                if data.get('vps_id') == self.vps_id and data["created_by"] == str(interaction.user.id):
                    vps = data
                    old_token = token
                    break

            if not vps:
                await interaction.response.send_message("❌ VPS not found or you don't have permission to transfer it!", ephemeral=True)
                return

            # Get old owner info
            try:
                old_owner = await bot.fetch_user(int(vps["created_by"]))
                old_owner_name = old_owner.name
            except:
                old_owner_name = "Unknown User"

            # Get new owner info
            try:
                new_owner = await bot.fetch_user(int(new_owner_id))
                new_owner_name = new_owner.name
            except:
                await interaction.response.send_message("❌ Invalid user ID or mention!", ephemeral=True)
                return

            # Update VPS ownership
            vps["created_by"] = str(new_owner.id)
            save_vps_data()

            # Send confirmation messages
            await interaction.response.send_message(f"✅ VPS {self.vps_id} has been transferred from {old_owner_name} to {new_owner_name}!", ephemeral=True)
            
            # Notify new owner
            try:
                embed = discord.Embed(title="VPS Transferred to You", color=discord.Color.green())
                embed.add_field(name="VPS ID", value=self.vps_id, inline=True)
                embed.add_field(name="Previous Owner", value=old_owner_name, inline=True)
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Access Token", value=old_token, inline=False)
                await new_owner.send(embed=embed)
            except:
                await interaction.followup.send("Note: Could not send DM to the new owner.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error transferring VPS: {str(e)}", ephemeral=True)

@bot.command(name='transfer_vps')
@commands.check(has_required_role)
async def transfer_vps_command(ctx, vps_id: str, new_owner: discord.Member):
    """Transfer a VPS to another user"""
    try:
        # Find the VPS
        vps = None
        old_token = None
        for token, data in vps_data.items():
            if data.get('vps_id') == vps_id and data["created_by"] == str(ctx.author.id):
                vps = data
                old_token = token
                break

        if not vps:
            await ctx.send("❌ VPS not found or you don't have permission to transfer it!")
            return

        # Update VPS ownership
        vps["created_by"] = str(new_owner.id)
        save_vps_data()

        # Send confirmation message
        await ctx.send(f"✅ VPS {vps_id} has been transferred from {ctx.author.name} to {new_owner.name}!")

        # Notify new owner
        try:
            embed = discord.Embed(title="VPS Transferred to You", color=discord.Color.green())
            embed.add_field(name="VPS ID", value=vps_id, inline=True)
            embed.add_field(name="Previous Owner", value=ctx.author.name, inline=True)
            embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
            embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
            embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
            embed.add_field(name="Username", value=vps['username'], inline=True)
            embed.add_field(name="Access Token", value=old_token, inline=False)
            await new_owner.send(embed=embed)
        except:
            await ctx.send("Note: Could not send DM to the new owner.")

    except Exception as e:
        await ctx.send(f"❌ Error transferring VPS: {str(e)}")

# Run the bot
if __name__ == "__main__":
    bot.run(TOKEN) 