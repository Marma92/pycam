#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace pycam
{

// Unix socket server speaking JSON lines, on its own thread.
// Incoming messages are handed to `handler` (called from the server thread);
// Send/Broadcast can be called from any thread.
class IpcServer
{
public:
	using Handler = std::function<void(int client, nlohmann::json message)>;
	static constexpr int kBroadcast = -1;

	~IpcServer();

	void Start(std::string const &path, Handler handler);
	void Stop();

	void Send(int client, nlohmann::json const &message);
	void Broadcast(nlohmann::json const &message) { Send(kBroadcast, message); }

private:
	struct Client
	{
		int fd;
		std::string in;
		std::string out;
	};

	void Loop();
	void Accept();
	bool Read(int id, Client &client);
	bool Flush(Client &client);
	void Wake();

	std::string path_;
	Handler handler_;
	int listen_fd_ = -1;
	int wake_fd_ = -1;
	std::map<int, Client> clients_;
	int next_client_ = 1;
	std::mutex outbox_mutex_;
	std::vector<std::pair<int, std::string>> outbox_;
	std::atomic<bool> stop_ { false };
	std::thread thread_;
};

} // namespace pycam
